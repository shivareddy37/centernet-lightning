from pytorch_lightning.cli import LightningCLI
from centernet_lightning.models.centernet import CenterNet

if __name__ == "__main__":
    cli = LightningCLI(CenterNet, save_config_overwrite=True)
