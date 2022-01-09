import warnings
from typing import Tuple, Iterable

import numpy as np
import cv2
import torch
import matplotlib.pyplot as plt
import pytorch_lightning as pl
from pytorch_lightning.loggers import TensorBoardLogger, WandbLogger
from torch.utils.data import Subset

try:
    import wandb
except:
    pass

# from ..datasets import IMAGENET_MEAN, IMAGENET_STD, build_dataset
from .box import convert_box_format


RED = (255,0,0)
GREEN = (0,255,0)
BLUE = (0,0,255)
WHITE = (255,255,255)
BLACK = (0,0,0)

def revert_imagenet_normalization(img):
    if isinstance(img, np.ndarray):     # NHWC or HWC
        mean = np.array(IMAGENET_MEAN)
        std = np.array(IMAGENET_STD)

    elif isinstance(img, torch.Tensor):
        mean = torch.tensor(IMAGENET_MEAN).view(3,1,1)  # CHW
        std = torch.tensor(IMAGENET_STD).view(3,1,1)

        if len(img.shape) == 4: # NCHW
            mean = mean.unsqueeze()
            std = std.unsqueeze()
        
    img = img * std + mean
    return img

def draw_boxes(img: np.ndarray, boxes, extra_texts=None, text_top=True, color=RED, text_color=WHITE, inplace=False):
    """Draw bounding boxes on an image using `cv2`
    
    Args:
        img: an RGB image in HWC format
        boxes: x1y1x2y2 format
        color: color used for bbox
        text_color for text (label and score)
        inplace: whether to draw bboxes directly on the original image or make a copy. Default is True
    """
    box_thickness = 5
    font = cv2.FONT_HERSHEY_SIMPLEX
    text_size = 2
    text_thickness = 2
    
    if not inplace:
        img = img.copy()

    boxes = [[round(x) for x in box] for box in boxes]

    if extra_texts is None:
        for x1,y1,x2,y2 in boxes:
            cv2.rectangle(img, (x1,y1), (x2,y2), color, box_thickness)

    else:
        for (x1,y1,x2,y2), text in zip(boxes, extra_texts):
            cv2.rectangle(img, (x1,y1), (x2,y2), color, box_thickness)

            (text_w, text_h), baseline = cv2.getTextSize(text, font, text_size, text_thickness)
            if text_top:
                text_pt = (x1, y1 + text_h)
                pt1 = (x1, y1 + text_h + baseline)
                pt2 = (x1 + text_w, y1)
            else:
                text_pt = (x1, y2 - baseline)
                pt1 = (x1, y2)
                pt2 = (x1 + text_w, y2 - text_h - baseline)
            cv2.rectangle(img, pt1, pt2, color, thickness=cv2.FILLED)
            cv2.putText(img, text, text_pt, font, text_size, text_color, thickness=text_thickness)

    return img

def draw_heatmap(img: np.ndarray, heatmap: np.ndarray, inplace: bool=True):
    """Draw heatmap on image. Both `img` and `heatmap` are in HWC format
    """
    if not inplace:
        img = img.copy()

    if heatmap.shape[-1] > 1:
        heatmap = np.max(heatmap, axis=-1)   # reduce to 1 channel

    # blend to first channel, using max
    img[:,:,0] = np.maximum(img[:,:,0], heatmap, out=img[:,:,0])
    return img

def apply_mpl_cmap(input: np.ndarray, cmap: str, return_tensor=False, channel_first=False):
    """input is a batch of 1-channel images. shape NHW (no channel dimension)
    """
    cm = plt.get_cmap(cmap)
    output = cm(input)[...,:3]  # apply cmap and remove alpha channel

    if channel_first:
        output = output.transpose(0,3,1,2)  # NHWC to NCHW
    if return_tensor:
        output = torch.from_numpy(output)
    return output

# class LogImageCallback(pl.Callback):
#     """Take a subset of `detection_file` based on the provided `indices`.
#     """
#     imagenet_mean = np.array(IMAGENET_MEAN, dtype=np.float32)
#     imagenet_std  = np.array(IMAGENET_STD, dtype=np.float32)
#     cmap = "viridis"

#     def __init__(self, dataset_cfg, indices = None, n_epochs=1, random=False):
#         super().__init__()
#         dataset = build_dataset(dataset_cfg)
        
#         if indices is None:
#             indices = 16
#         if isinstance(indices, int):
#             indices = np.random.randint(len(dataset), size=indices) if random else range(indices)

#         dataset = Subset(dataset, indices)
#         self.dataset = dataset
#         self.n_epochs = n_epochs

#     # log target heatmap on fit start
#     def on_fit_start(self, trainer: pl.Trainer, pl_module: pl.LightningModule):
#         _, img_height, img_width = self.dataset[0]["image"].shape
#         head = pl_module.output_heads["heatmap"]
        
#         heatmap_height = img_height // pl_module.output_stride
#         heatmap_width = img_width // pl_module.output_stride
#         heatmap_shape = (1, head.num_classes, heatmap_height, heatmap_width)
#         heatmaps = []

#         # render target heatmap
#         for item in self.dataset:
#             bboxes = torch.tensor(item["bboxes"]).unsqueeze(0)
#             labels = torch.tensor(item["labels"]).unsqueeze(0)
#             mask = torch.ones_like(labels)
#             heatmap = head._render_target_heatmap(heatmap_shape, bboxes, labels, mask, device="cpu").squeeze(0).numpy()

#             heatmap = np.max(heatmap, axis=0)
#             heatmap = apply_mpl_cmap(heatmap, self.cmap)
#             heatmaps.append(heatmap)

#         # make into a grid and log the image
#         heatmap_grid = make_image_grid(heatmaps)
#         if isinstance(trainer.logger, WandbLogger):         
#             trainer.logger.experiment.log({"target heatmap": wandb.Image(heatmap_grid)})
#         elif isinstance(trainer.logger, TensorBoardLogger):
#             trainer.logger.experiment.add_image("target heatmap", heatmap_grid, dataformats="hwc")

#     # run inference and log predicted detections
#     @torch.no_grad()
#     def on_epoch_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule):
#         if pl_module.current_epoch % self.n_epochs != 0:
#             return

#         log_images = {
#             "heatmap": [],
#             "heatmap (scaled)": [],
#             "features": []
#         }
#         images = []
#         detections_target = {
#             "bboxes": [],
#             "labels": []
#         }
#         detections_pred = {
#             "bboxes": [],
#             "labels": [],
#             "scores": []
#         }
#         _, img_height, img_width = self.dataset[0]["image"].shape

#         pl_module.eval()
#         for item in self.dataset:
#             # save image to make a grid later
#             img = item["image"]
#             # img_np = img.clone().numpy().transpose(1,2,0) * self.imagenet_std + self.imagenet_mean
#             img_np = img.clone().numpy().transpose(1,2,0)
#             images.append(img_np)
            
#             # save ground truth bboxes and labels
#             for k in detections_target.keys():
#                 detections_target[k].append(np.array(item[k]))
            
#             detections_target["bboxes"][-1][...,[0,2]] *= img_width
#             detections_target["bboxes"][-1][...,[1,3]] *= img_height
#             detections_target["bboxes"][-1] = convert_box_format(detections_target["bboxes"][-1], "cxcywh", "xyxxy")

#             img = img.unsqueeze(0).to(pl_module.device)
#             encoded_outputs = pl_module.get_encoded_outputs(img)
#             heatmap = torch.sigmoid(encoded_outputs["heatmap"])
#             box_2d = encoded_outputs["box_2d"]
#             pred_detections = pl_module.gather_detection2d(heatmap, box_2d)

#             for k in detections_pred.keys():
#                 detections_pred[k].append(pred_detections[k][0].cpu().numpy())

#             # log heatmap output
#             heatmap = heatmap[0].cpu().float()          # 80 x 128 x 128
#             heatmap, _ = torch.max(heatmap, dim=0)      # 128 x 128
#             heatmap_scaled = heatmap / torch.max(heatmap)

#             heatmap = apply_mpl_cmap(heatmap.numpy(), self.cmap)  # 128 x 128 x 3
#             log_images["heatmap"].append(heatmap)

#             heatmap_scaled = apply_mpl_cmap(heatmap_scaled.numpy(), self.cmap)
#             log_images["heatmap (scaled)"].append(heatmap_scaled)

#             # log backbone output
#             features = encoded_outputs["features"][0].cpu().float()
#             features = torch.mean(features, dim=0)
#             features = apply_mpl_cmap(features.numpy(), self.cmap)
#             log_images["features"].append(features)

#         img_grid, target_bboxes, pred_bboxes = make_image_grid(images, detections_target["bboxes"], detections_pred["bboxes"])
#         pred_labels = np.concatenate(detections_pred["labels"], axis=0)
#         pred_scores = np.concatenate(detections_pred["scores"], axis=0)
#         target_labels = np.concatenate(detections_target["labels"], axis=0)

#         log_images = {k: make_image_grid(v) for k,v in log_images.items()}

#         if isinstance(trainer.logger, WandbLogger):
#             wandb_log = {
#                 "detections": wandb.Image(img_grid, boxes={
#                     "predictions": {"box_data": convert_bboxes_to_wandb(pred_bboxes, pred_labels, pred_scores)},
#                     "ground_truth": {"box_data": convert_bboxes_to_wandb(target_bboxes, target_labels, np.ones(len(target_labels)))}
#                 }),
#                 "global_step": trainer.global_step
#             }

#             for name, img in log_images.items():
#                 wandb_log[name] = wandb.Image(img)
            
#             trainer.logger.experiment.log(wandb_log)

#         elif isinstance(trainer.logger, TensorBoardLogger):
#             draw_boxes(img_grid, pred_bboxes, pred_labels, pred_scores, color=RED)
#             draw_boxes(img_grid, target_bboxes, target_labels, color=BLUE)
#             trainer.logger.experiment.add_image("detections", img_grid, trainer.global_step, dataformats="hwc")

#             for name, img in log_images.items():
#                 trainer.logger.experiment.add_image(name, img, trainer.global_step, dataformats="hwc")

# def make_image_grid(imgs: Iterable[np.ndarray], bboxes1: Iterable[np.ndarray] = None, bboxes2: Iterable[np.ndarray] = None, imgs_per_row: int = 8):
#     """
#     Args
#         imgs: a list of images in HWC format
#         bboxes1 and bboxes2 (optional): a list of bboxes. intended for ground truth boxes and predicted boxes
#     """
#     num_imgs = len(imgs)
#     img_height, img_width, channels = imgs[0].shape
#     if bboxes1 is not None:
#         bboxes1 = [bboxes.copy() for bboxes in bboxes1]
#     if bboxes2 is not None:
#         bboxes2 = [bboxes.copy() for bboxes in bboxes2]

#     num_rows = np.ceil(num_imgs / imgs_per_row).astype(int)
#     grid = np.zeros((num_rows*img_height, imgs_per_row*img_width, channels), dtype=imgs[0].dtype)

#     for i, img in enumerate(imgs):
#         y = i // imgs_per_row
#         x = i % imgs_per_row
#         grid[y*img_height:(y+1)*img_height, x*img_width:(x+1)*img_width] = img
        
#         # shift the bboxes
#         if bboxes1 is not None:
#             bboxes1[i][:,[0,2]] += x * img_width
#             bboxes1[i][:,[1,3]] += y * img_height
#         if bboxes2 is not None:
#             bboxes2[i][:,[0,2]] += x * img_width
#             bboxes2[i][:,[1,3]] += y * img_height

#     # combine bboxes of several images into a single list of bboxes
#     if bboxes1 is not None:
#         bboxes1 = np.concatenate(bboxes1, axis=0)
#     if bboxes2 is not None:
#         bboxes2 = np.concatenate(bboxes2, axis=0)

#     if bboxes1 is not None:
#         if bboxes2 is not None:
#             return grid, bboxes1, bboxes2
#         return grid, bboxes1
#     return grid

# def convert_bboxes_to_wandb(bboxes: np.ndarray, labels: np.ndarray, scores: np.ndarray = None):
#     """
#     """
#     wandb_boxes = []
#     bboxes = bboxes.astype(int)
#     labels = labels.astype(int)

#     for i in range(len(labels)):
#         item = {
#             "position": {
#                 "minX": bboxes[i][0].item(),
#                 "minY": bboxes[i][1].item(),
#                 "maxX": bboxes[i][2].item(),
#                 "maxY": bboxes[i][3].item()
#             },
#             "domain": "pixel",
#             "class_id": labels[i].item()
#         }
#         if scores is not None:
#             item["scores"] = {"confidence": scores[i].item()}
        
#         wandb_boxes.append(item)
    
#     return wandb_boxes
