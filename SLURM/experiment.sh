#!/bin/bash

# Command line args for dict
sbatch <<EOT
#!/bin/bash
#SBATCH --job-name=DeepTreeAttention   # Job name
#SBATCH --mail-type=END               # Mail events
#SBATCH --mail-user=riteshchoudhery313@gmail.com  # Where to send mail
#SBATCH --account=azare
#SBATCH --nodes=1                 # Number of MPI ran
#SBATCH --cpus-per-task=20
#SBATCH --mem=50GB
#SBATCH --time=48:00:00       #Time limit hrs:min:sec
#SBATCH --output=/home/riteshchowdhry/logs/DeepTreeAttention_%j.out   # Standard output and error log
#SBATCH --error=/home/riteshchowdhry/logs/DeepTreeAttention_%j.err
#SBATCH --partition=gpu
#SBATCH --gpus=1

ulimit -c 0

source activate DeepTreeAttention

cd ~/DeepTreeAttention/
module load git gcc
python train.py $1
EOT

