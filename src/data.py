#Ligthning data module
import argparse
from . import __file__
from distributed import as_completed
import glob
import geopandas as gpd
import json
import numpy as np
import os
import pandas as pd
from pytorch_lightning import LightningDataModule
import rasterio as rio
from sklearn import preprocessing
from src import generate
from src import CHM
from src import augmentation
from shapely.geometry import Point
import torch
from torch.utils.data import Dataset
from torchvision import transforms
import yaml
import warnings
        
def filter_data(path, config):
    """Transform raw NEON data into clean shapefile   
    Args:
        config: DeepTreeAttention config dict, see config.yml
    """
    field = pd.read_csv(path)
    field = field[~field.elevation.isnull()]
    field = field[~field.growthForm.isin(["liana","small shrub"])]
    field = field[~field.growthForm.isnull()]
    field = field[~field.plantStatus.isnull()]        
    field = field[field.plantStatus.str.contains("Live")]    
    
    groups = field.groupby("individualID")
    shaded_ids = []
    for name, group in groups:
        shaded = any([x in ["Full shade", "Mostly shaded"] for x in group.canopyPosition.values])
        if shaded:
            if any([x in ["Open grown", "Full sun"] for x in group.canopyPosition.values]):
                continue
            else:
                shaded_ids.append(group.individualID.unique()[0])
        
    field = field[~(field.individualID.isin(shaded_ids))]
    field = field[(field.height > 3) | (field.height.isnull())]
    field = field[field.stemDiameter > config["min_stem_diameter"]]
    field = field[~field.taxonID.isin(["BETUL", "FRAXI", "HALES", "PICEA", "PINUS", "QUERC", "ULMUS", "2PLANT"])]
    field = field[~(field.eventID.str.contains("2014"))]
    with_heights = field[~field.height.isnull()]
    with_heights = with_heights.loc[with_heights.groupby('individualID')['height'].idxmax()]
    
    missing_heights = field[field.height.isnull()]
    missing_heights = missing_heights[~missing_heights.individualID.isin(with_heights.individualID)]
    missing_heights = missing_heights.groupby("individualID").apply(lambda x: x.sort_values(["eventID"],ascending=False).head(1)).reset_index(drop=True)
  
    field = pd.concat([with_heights,missing_heights])
    
    #remove multibole
    field = field[~(field.individualID.str.contains('[A-Z]$',regex=True))]

    #List of hand cleaned errors
    known_errors = ["NEON.PLA.D03.OSBS.03422","NEON.PLA.D03.OSBS.03422","NEON.PLA.D03.OSBS.03382", "NEON.PLA.D17.TEAK.01883"]
    field = field[~(field.individualID.isin(known_errors))]
    field = field[~(field.plotID == "SOAP_054")]
    
    #Create shapefile
    field["geometry"] = [Point(x,y) for x,y in zip(field["itcEasting"], field["itcNorthing"])]
    shp = gpd.GeoDataFrame(field)
    
    #HOTFIX, BLAN has some data in 18N UTM, reproject to 17N update columns
    BLAN_errors = shp[(shp.siteID == "BLAN") & (shp.utmZone == "18N")]
    BLAN_errors.set_crs(epsg=32618, inplace=True)
    BLAN_errors.to_crs(32617,inplace=True)
    BLAN_errors["utmZone"] = "17N"
    BLAN_errors["itcEasting"] = BLAN_errors.geometry.apply(lambda x: x.coords[0][0])
    BLAN_errors["itcNorthing"] = BLAN_errors.geometry.apply(lambda x: x.coords[0][1])
    
    #reupdate
    shp.loc[BLAN_errors.index] = BLAN_errors
    
    #Oak Right Lab has no AOP data
    shp = shp[~(shp.siteID.isin(["PUUM","ORNL"]))]

    return shp

def sample_plots(shp, test_fraction=0.1, min_samples=5):
    """Sample and split a pandas dataframe based on plotID
    Args:
        shp: pandas dataframe of filtered tree locations
        test_fraction: proportion of plots in test datasets
        min_samples: minimum number of samples per class
    """
    #split by plot level
    test_plots = shp.plotID.drop_duplicates().sample(frac=test_fraction)
    
    #in case of debug, there may be not enough plots to sample, grab the first for testing
    if test_plots.empty:
        test_plots = [shp.plotID.drop_duplicates().values[0]]
    
    test = shp[shp.plotID.isin(test_plots)]
    train = shp[~shp.plotID.isin(test_plots)]
    
    test = test.groupby("taxonID").filter(lambda x: x.shape[0] > min_samples)
    
    train = train[train.taxonID.isin(test.taxonID)]
    test = test[test.taxonID.isin(train.taxonID)]
    
    return train, test
    
def train_test_split(shp, savedir, config, client = None):
    """Create the train test split
    Args:
        shp: a filter pandas dataframe (or geodataframe)  
        savedir: directly to save train/test and metadata csv files
        client: optional dask client
    Returns:
        None: train.shp and test.shp are written as side effect
        """    
    #set seed.
    np.random.seed(1)
    most_species = 0
    if client:
        futures = [ ]
        for x in np.arange(config["iterations"]):
            future = client.submit(sample_plots, shp=shp, min_samples=config["min_samples"], test_fraction=config["test_fraction"])
            futures.append(future)
        
        for x in as_completed(futures):
            train, test = x.result()
            if len(train.taxonID.unique()) > most_species:
                print(len(train.taxonID.unique()))
                saved_train = train
                saved_test = test
                most_species = len(train.taxonID.unique())            
    else:
        for x in np.arange(config["iterations"]):
            train, test = sample_plots(shp, min_samples=config["min_samples"], test_fraction=config["test_fraction"])
            if len(train.taxonID.unique()) > most_species:
                print(len(train.taxonID.unique()))
                saved_train = train
                saved_test = test
                most_species = len(train.taxonID.unique())
    
    train = saved_train
    test = saved_test    
    
    print("There are {} records for {} species for {} sites in filtered train".format(
        train.shape[0],
        len(train.taxonID.unique()),
        len(train.siteID.unique())
    ))
    
    print("There are {} records for {} species for {} sites in test".format(
        test.shape[0],
        len(test.taxonID.unique()),
        len(test.siteID.unique())
    ))
    
    #Give tests a unique index to match against
    test["point_id"] = test.index.values
    train["point_id"] = train.index.values
    
    return train, test
        
def read_config(config_path):
    """Read config yaml file"""
    
    #Allow command line to override 
    parser = argparse.ArgumentParser("DeepTreeAttention config")
    parser.add_argument('-d', '--my-dict', type=json.loads, default=None)
    args = parser.parse_known_args()
    try:
        with open(config_path, 'r') as f:
            config = yaml.load(f, Loader=yaml.FullLoader)

    except Exception as e:
        raise FileNotFoundError("There is no config at {}, yields {}".format(
            config_path, e))
    
    #Update anything in argparse to have higher priority
    if args[0].my_dict:
        for key, value in args[0].my_dict:
            config[key] = value
        
    return config

def preprocess_image(image, channel_is_first=False):
    """Preprocess a loaded image, if already C*H*W set channel_is_first=True"""
    img = np.asarray(image, dtype='float32')
    data = img.reshape(img.shape[0], np.prod(img.shape[1:]))
    
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)    
        data  = preprocessing.scale(data)
    img = data.reshape(img.shape)
    
    if not channel_is_first:
        img = np.rollaxis(img, 2,0)
        
    normalized = torch.from_numpy(img)
    
    return normalized

def load_image(img_path, image_size):
    """Load and preprocess an image for training/prediction"""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', rio.errors.NotGeoreferencedWarning)
        image = rio.open(img_path).read()       
    image = preprocess_image(image, channel_is_first=True)
    
    #resize image
    image = transforms.functional.resize(image, size=(image_size,image_size), interpolation=transforms.InterpolationMode.NEAREST)
    
    return image

#Dataset class
class TreeDataset(Dataset):
    """A csv file with a path to image crop and label
    Args:
       csv_file: path to csv file with image_path and label
    """
    def __init__(self, csv_file, image_size=10, config=None, train=True, HSI=True, metadata=False):
        self.annotations = pd.read_csv(csv_file)
        self.train = train
        self.HSI = HSI
        self.metadata = metadata
        
        if config:
            self.image_size = config["image_size"]
        else:
            self.image_size = image_size
        
        #Create augmentor
        self.transformer = augmentation.train_augmentation(image_size=image_size)
            
    def __len__(self):
        return self.annotations.shape[0]
        
    def __getitem__(self, index):
        inputs = {}
        image_path = self.annotations.image_path.loc[index]      
        individual = os.path.basename(image_path.split(".tif")[0])
        if self.HSI:
            image_path = self.annotations.image_path.loc[index]            
            image = load_image(image_path, image_size=self.image_size)
            inputs["HSI"] = image
            
        if self.metadata:
            site = self.annotations.site.loc[index]  
            site = torch.tensor(site, dtype=torch.int)
            inputs["site"] = site
        
        if self.train:
            label = self.annotations.label.loc[index]
            label = torch.tensor(label, dtype=torch.long)
            
            if self.HSI:
                image = self.transformer(image)
                inputs["HSI"] = image

            return individual, inputs, label
        else:
            return individual, inputs

class TreeData(LightningDataModule):
    """
    Lightning data module to convert raw NEON data into HSI pixel crops based on the config.yml file. 
    The module checkpoints the different phases of setup, if one stage failed it will restart from that stage. 
    Use regenerate=True to override this behavior in setup()
    """
    def __init__(self, csv_file, HSI=True, metadata=False, regenerate = False, client = None, config=None, data_dir=None):
        """
        Args:
            config: optional config file to override
            data_dir: override data location, defaults to ROOT   
            regenerate: Whether to recreate raw data
        """
        super().__init__()
        self.ROOT = os.path.dirname(os.path.dirname(__file__))
        self.regenerate=regenerate
        self.csv_file = csv_file
        self.HSI = HSI
        self.metadata = metadata
        
        #default training location
        self.client = client
        if data_dir is None:
            self.data_dir = "{}/data/".format(self.ROOT)
        else:
            self.data_dir = data_dir            
        
        self.train_file = "{}/processed/train.csv".format(self.data_dir)
        
        if config is None:
            self.config = read_config("{}/config.yml".format(self.ROOT))   
        else:
            self.config = config
                
    def setup(self,stage=None):
        #Clean data from raw csv, regenerate from scratch or check for progress and complete
        if self.regenerate:
            #remove any previous runs
            try:
                os.remove("{}/processed/test_points.shp".format(self.data_dir))
                os.remove(" ".format(self.data_dir))
                os.remove("{}/processed/filtered_data.csv".format(self.data_dir))
                os.remove("{}/processed/train_crowns.shp".format(self.data_dir))
                for x in glob.glob(self.config["crop_dir"]):
                    os.remove(x)
            except:
                pass
                
            #Convert raw neon data to x,y tree locatins
            df = filter_data(self.csv_file, config=self.config)
            
            #DEBUG, just one site
            #df = df[df.siteID=="HARV"]
            
            #Filter points based on LiDAR height
            df = CHM.filter_CHM(df, CHM_pool=self.config["CHM_pool"],min_CHM_diff=self.config["min_CHM_diff"], min_CHM_height=self.config["min_CHM_height"])      
            df = df.groupby("taxonID").filter(lambda x: x.shape[0] > self.config["min_samples"])
            train, test = train_test_split(df,savedir="{}/processed".format(self.data_dir),config=self.config, client=None)   
            
            test.to_file("{}/processed/test_points.shp".format(self.data_dir))
            train.to_file("{}/processed/train_points.shp".format(self.data_dir))
            
            #Store class labels
            unique_species_labels = np.concatenate([train.taxonID.unique(), test.taxonID.unique()])
            unique_species_labels = np.unique(unique_species_labels)
            self.num_classes = len(unique_species_labels)
            
            self.species_label_dict = {}
            for index, label in enumerate(unique_species_labels):
                self.species_label_dict[label] = index
        
            #Store site labels
            unique_site_labels = np.concatenate([train.siteID.unique(), test.siteID.unique()])
            unique_site_labels = np.unique(unique_site_labels)
            
            self.site_label_dict = {}
            for index, label in enumerate(unique_site_labels):
                self.site_label_dict[label] = index
            self.num_sites = len(self.site_label_dict)        
            
            #test data 
            train_crowns = generate.points_to_crowns(
                field_data="{}/processed/train_points.shp".format(self.data_dir),
                rgb_dir=self.config["rgb_sensor_pool"],
                savedir=None,
                raw_box_savedir=None, 
                client=self.client
            )
            
            train_crowns.to_file("{}/processed/train_crowns.shp".format(self.data_dir))
            
            train_annotations = generate.generate_crops(
                train_crowns,
                savedir=self.config["crop_dir"],
                label_dict=self.species_label_dict,
                site_dict=self.site_label_dict,                
                sensor_glob=self.config["HSI_sensor_pool"],
                convert_h5=self.config["convert_h5"],   
                rgb_glob=self.config["rgb_sensor_pool"],
                HSI_tif_dir=self.config["HSI_tif_dir"],
                client=self.client
            )            
                        
            test_crowns = generate.points_to_crowns(
                field_data="{}/processed/test_points.shp".format(self.data_dir),
                rgb_dir=self.config["rgb_sensor_pool"],
                savedir=None,
                raw_box_savedir=None, 
                client=self.client
            )
            test_crowns.to_file("{}/processed/test_crowns.shp".format(self.data_dir))
            
            test_annotations = generate.generate_crops(
                test_crowns,
                savedir=self.config["crop_dir"],
                label_dict=self.species_label_dict,
                site_dict=self.site_label_dict,
                sensor_glob=self.config["HSI_sensor_pool"],
                rgb_glob=self.config["rgb_sensor_pool"],                
                client=self.client,
                HSI_tif_dir=self.config["HSI_tif_dir"],                
                convert_h5=self.config["convert_h5"]
            )  
            
            #Make sure no species were lost during generate
            train_annotations = train_annotations[train_annotations.label.isin(test_annotations.label.unique())]
            
            train_annotations.to_csv("{}/processed/train.csv".format(self.data_dir), index=False)            
            test_annotations.to_csv("{}/processed/test.csv".format(self.data_dir), index=False)
        else:
            test = gpd.read_file("{}/processed/test_points.shp".format(self.data_dir))
            train = gpd.read_file("{}/processed/train_points.shp".format(self.data_dir))
            
            #Store class labels
            unique_species_labels = np.concatenate([train.taxonID.unique(), test.taxonID.unique()])
            unique_species_labels = np.unique(unique_species_labels)
            
            self.species_label_dict = {}
            for index, label in enumerate(unique_species_labels):
                self.species_label_dict[label] = index
            self.num_classes = len(self.species_label_dict)
            
            #Store site labels
            unique_site_labels = np.concatenate([train.siteID.unique(), test.siteID.unique()])
            unique_site_labels = np.unique(unique_site_labels)
            
            self.site_label_dict = {}
            for index, label in enumerate(unique_site_labels):
                self.site_label_dict[label] = index
            self.num_sites = len(self.site_label_dict)
            
    def resample(self, csv_file, oversample=True):
        """Given the processed train/test split, resample to reduce class imbalance. This function honors the min and max sampling size from the .config
        In order to prioritize a diverse training site, individual crops are first chosen by site and then individual
        
        Args:
            csv: path to .csv file containing training annotations
            oversample (True): Whether to duplicate rare classes to to the mininum threshold specified in the .config
        Returns:
            resampled_species: pandas dataframe with image_path and label to each pixel crop
        """
        train = pd.read_csv(csv_file)
        train["individual"] = train.image_path.apply(lambda x: os.path.basename(x).split("_")[0]) 
        labels = train.label.unique()
        resampled_species = []
        selected_indices = []
        for x in labels:
            species_samples = train[train.label == x]
            if species_samples.shape[0] <= self.config["resample_max"]:
                for index, row in species_samples.iterrows():
                    resampled_species.append(row)
            else:       
                counter = 0
                sites = species_samples["site"].unique()
                while counter < self.config["resample_max"]:
                    for x in sites:
                        site_samples = species_samples[species_samples.site == x]
                        i = site_samples.individual.sample(1)
                        selected_individual = species_samples[species_samples.individual.isin(i)]
                        for index, row in selected_individual.iterrows():
                            if not index in selected_indices:
                                resampled_species.append(row)
                                selected_indices.append(index)
                                counter = counter + 1
                                break
            
        resampled_species = pd.DataFrame(resampled_species)
        
        #Oversampling of rare classes
        if oversample:
            label_counts = resampled_species.label.value_counts() 
            rare_index = label_counts[label_counts < self.config["resample_min"]].index
            if not rare_index.shape[0] == 0:   
                to_be_oversampled = resampled_species[resampled_species.label.isin(rare_index)]
                not_to_be_oversampled = resampled_species[~resampled_species.label.isin(rare_index)]
                
                oversampled = to_be_oversampled.groupby("label").sample(n=self.config["resample_min"], replace=True)
                resampled_species = pd.concat([oversampled,not_to_be_oversampled])
                
        return resampled_species  

    def train_dataloader(self):
        """Load a training file. The default location is saved during self.setup(), to override this location, set self.train_file before training"""
        ds = TreeDataset(csv_file = self.train_file, config=self.config, HSI=self.HSI, metadata=self.metadata)
        data_loader = torch.utils.data.DataLoader(
            ds,
            batch_size=self.config["batch_size"],
            shuffle=True,
            num_workers=self.config["workers"],
        )
        
        return data_loader
    
    def val_dataloader(self):
        ds = TreeDataset(csv_file = "{}/processed/test.csv".format(self.data_dir), config=self.config, HSI=self.HSI, metadata=self.metadata)
        data_loader = torch.utils.data.DataLoader(
            ds,
            batch_size=self.config["batch_size"],
            shuffle=False,
            num_workers=self.config["workers"],
        )
        
        return data_loader