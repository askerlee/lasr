from typing import List
import numpy as np
from retinaface.pre_trained_models import get_model
from typing import List, Tuple, Optional
import torch
from torch import nn
from torch.nn import functional as F

# Copied from deepface/models/Detector.py
class FacialAreaRegion:
    x: int
    y: int
    w: int
    h: int
    left_eye: Tuple[int, int]
    right_eye: Tuple[int, int]
    confidence: float

    def __init__(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        left_eye: Optional[Tuple[int, int]] = None,
        right_eye: Optional[Tuple[int, int]] = None,
        nose: Optional[Tuple[int, int]] = None,
        confidence: Optional[float] = None,
    ):
        """
        Initialize a Face object.

        Args:
            x (int): The x-coordinate of the top-left corner of the bounding box.
            y (int): The y-coordinate of the top-left corner of the bounding box.
            w (int): The width of the bounding box.
            h (int): The height of the bounding box.
            left_eye (tuple): The coordinates (x, y) of the left eye with respect to
                the person instead of observer. Default is None.
            right_eye (tuple): The coordinates (x, y) of the right eye with respect to
                the person instead of observer. Default is None.
            confidence (float, optional): Confidence score associated with the face detection.
                Default is None.
        """
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.left_eye = left_eye
        self.right_eye = right_eye
        self.nose = nose
        self.mouth_left  = None
        self.mouth_right = None
        self.confidence  = confidence

class RetinaFaceClient(nn.Module):
    def __init__(self, device='cuda'):
        super(RetinaFaceClient, self).__init__()
        # We have called torch.cuda.set_device(opt.gpu) in stable_txt2img.py, so to("cuda") 
        # will put the model on the correct GPU.
        self.model = get_model("biubug6", max_size=1024, device=device)

    def detect_faces(self, img: np.ndarray, T=20) -> List[FacialAreaRegion]:
        """
        Detect and align face with retinaface

        Args:
            img (np.ndarray): pre-loaded image as numpy array,
            T: minimum size of the face (the height or the width) to be detected

        Returns:
            results (List[FacialAreaRegion]): A list of FacialAreaRegion objects
        """
        resp = []

        # predict_jsons is wrapped with torch.no_grad().
        objs = self.model.predict_jsons(img, confidence_threshold=0.9)
        H, W = img.shape[:2]

        '''
            {'bbox': [78.36, 113.11, 276.53, 375.64], 'score': 1.0, 
            'landmarks': [[130.65, 218.37], [224.65, 214.67], [182.26, 260.41], [140.13, 305.17], [227.34, 301.35]]}        
        '''
        for identity in objs:
            detection = identity["bbox"]
            if len(detection) != 4:
                # No face detected
                continue

            # clip detection box to image size
            y1, y2 = max(detection[1], 0), min(detection[3], H)
            x1, x2 = max(detection[0], 0), min(detection[2], W)
            h = y2 - y1
            w = x2 - x1

            if h <= T or w <= T:
                # Face too small
                continue

            # retinaface sets left and right eyes with respect to the person
            # The landmark seems to be mirrored compared with deepface detectors.
            # Returns 5-point facial landmarks: right eye, left eye, nose, right mouth, left mouth
            left_eye  = identity["landmarks"][1]
            right_eye = identity["landmarks"][0]
            nose      = identity["landmarks"][2]

            # eyes are list of float, need to cast them tuple of int
            left_eye = tuple(int(i) for i in left_eye)
            right_eye = tuple(int(i) for i in right_eye)
            #print("left_eye: ", left_eye)
            #print("right_eye: ", right_eye)

            confidence = identity["score"]

            facial_area = FacialAreaRegion(
                x=x1,
                y=y1,
                w=w,
                h=h,
                left_eye=left_eye,
                right_eye=right_eye,
                nose=nose,
                confidence=confidence,
            )

            resp.append(facial_area)

        return resp

    # Find facial areas of given image tensors and crop them.
    # images_ts: typically [BS, 3, 512, 512] from diffusion (could be any sizes).
    # Output: [BS, 3, 128, 128] (cropped faces resized to 128x128), failed_indices, face_coords
    def crop_faces(self, images_ts, out_size=(128, 128), T=20, bleed=0, 
                   use_whole_image_if_no_face=False):
        face_crops      = []
        failed_indices  = []
        face_coords     = []
    
        for i, image_ts in enumerate(images_ts):
            # [3, H, W] -> [H, W, 3]
            image_np = image_ts.detach().cpu().numpy().transpose(1, 2, 0)
            # [-1, 1] -> [0, 255]
            image_np = ((image_np + 1) * 127.5).astype(np.uint8)

            # .detect_faces() doesn't require grad. So we convert the image tensor to numpy.
            faces = self.detect_faces(image_np, T=T)
            if len(faces) == 0:
                if use_whole_image_if_no_face:
                    face_crop = image_ts
                    face_coords.append((0, 0, image_ts.shape[2], image_ts.shape[1]))
                else:
                    # No face detected
                    failed_indices.append(i)
                    face_coords.append((0, 0, 0, 0))
                    continue
            else:
                max_facial_area = 0
                x_start_max = x_end_max = y_start_max = y_end_max = 0

                # Find the largest facial area
                for face in faces:
                    x = face.x
                    y = face.y
                    w = face.w
                    h = face.h

                    y_start = max(0, int(y + bleed))
                    y_end   = min(image_ts.shape[1], int(y + h - bleed))
                    x_start = max(0, int(x + bleed))
                    x_end   = min(image_ts.shape[2], int(x + w - bleed))

                    if (y_end - y_start) * (x_end - x_start) > max_facial_area:
                        max_facial_area = (y_end - y_start) * (x_end - x_start)
                        y_start_max = y_start
                        y_end_max   = y_end
                        x_start_max = x_start
                        x_end_max   = x_end

                if y_start_max + T >= y_end_max or x_start_max + T >= x_end_max:
                    # After trimming bleed pixels, the face is < T, too small.
                    failed_indices.append(i)
                    face_coords.append((0, 0, 0, 0))
                    continue

                # Extract detected face without alignment
                # Crop on the input tensor, so that computation graph is preserved.
                face_crop = image_ts[:, y_start_max:y_end_max, x_start_max:x_end_max]
                face_coords.append((x_start_max, y_start_max, x_end_max, y_end_max))

            # resize to (1, 3, 128, 128)
            face_crop = F.interpolate(face_crop.unsqueeze(0), size=out_size, mode='bilinear', align_corners=False)
            face_crops.append(face_crop)
        
        # face_coords: long tensor of [BS, 4]
        face_coords    = torch.tensor(face_coords,    device=images_ts.device)
        failed_indices = torch.tensor(failed_indices, device=images_ts.device)
        
        if len(face_crops) == 0:
            return None, failed_indices, face_coords
        
        face_crops = torch.cat(face_crops, dim=0)
        return face_crops, failed_indices, face_coords

