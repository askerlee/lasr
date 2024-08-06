import os
# Suppress tensorflow info and warning messages. This should be before importing deepface.
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

import torch
import re
import glob
import time
import numpy as np
from PIL import Image
import cv2

from evaluation.clip_eval import ImageDirEvaluator
from evaluation.dino_eval import DINOEvaluator
from evaluation.community_prompts import community_prompt_list
from insightface.app import FaceAnalysis
from ldm.data.personalized import PersonalizedBase

def set_tf_gpu(gpu_id):
    import tensorflow as tf
    if gpu_id >= 0:
        gpus = tf.config.experimental.list_physical_devices('GPU')
        if gpus:
            try:
                tf.config.experimental.set_visible_devices(gpus[gpu_id], 'GPU')
                tf.config.experimental.set_memory_growth(gpus[gpu_id], True)
            except RuntimeError as e:
                print(e)
    else:
        cpus = tf.config.experimental.list_physical_devices(device_type='CPU')
        tf.config.experimental.set_visible_devices(devices=cpus, device_type='CPU')
        tf.config.experimental.set_visible_devices([], 'GPU')

def init_evaluators(gpu_id):
    if gpu_id == -1:
        device = 'cpu'
    else:
        device = f'cuda:{gpu_id}'
    clip_evator = ImageDirEvaluator(device)
    dino_evator = DINOEvaluator(device)
    return clip_evator, dino_evator

# gt_dir: the reference image folder, or the path of a single reference image.
# num_samples: only evaluate the last (latest) num_samples images. 
# If -1, then evaluate all images
def compare_folders(clip_evator, dino_evator, gt_dir, samples_dir, prompt, num_samples=-1, gt_self_compare=False):
    # Import here to avoid recursive import.
    gt_data_loader         = PersonalizedBase(gt_dir,      set_name='evaluation', size=256, max_num_images_per_subject=-1, flip_p=0.0)
    if gt_self_compare:
        sample_data_loader = gt_data_loader
        # Always compare all images in the subject gt image folder.
        num_samples = -1
    else:
        sample_data_loader = PersonalizedBase(samples_dir, set_name='evaluation', size=256, max_num_images_per_subject=-1, flip_p=0.0)
        
    sample_start_idx = 0 if num_samples == -1 else sample_data_loader.num_images - num_samples
    sample_range     = range(sample_start_idx, sample_data_loader.num_images)

    gt_images = [torch.from_numpy(gt_data_loader[i]["image"]).permute(2, 0, 1) for i in range(gt_data_loader.num_images)]
    gt_images = torch.stack(gt_images, axis=0)
    sample_images = [torch.from_numpy(sample_data_loader[i]["image"]).permute(2, 0, 1) for i in sample_range]
    sample_images = torch.stack(sample_images, axis=0)
    print(f"prompt: {prompt}")
    #sample_image_paths = [sample_data_loader[i]["image_path"] for i in sample_range]
    #print("Sampel paths:", sample_image_paths)

    with torch.no_grad():
        sim_img, sim_text = clip_evator.evaluate(sample_images, gt_images, prompt)

    # huggingface's ViT pipeline requires a list of PIL images or numpy images as input.
    # "image_unnorm": unnormalized numpy array image in [0, 255]
    gt_np_images     = [ gt_data_loader[i]["image_unnorm"]     for i in range(gt_data_loader.num_images) ]
    sample_np_images = [ sample_data_loader[i]["image_unnorm"] for i in sample_range ]

    with torch.no_grad():
        sim_dino = dino_evator.image_pairwise_similarity(gt_np_images, sample_np_images)

    if gt_dir[-1] == "/":
        gt_dir = gt_dir[:-1]
    if samples_dir[-1] == "/":
        samples_dir = samples_dir[:-1]
    gt_dir_base = os.path.basename(gt_dir)
    samples_dir_base = os.path.basename(samples_dir)

    print(gt_dir_base, "vs", samples_dir_base)
    print(f"Image/text/dino sim: {sim_img:.3f} {sim_text:.3f} {sim_dino:.3f}")
    return sim_img, sim_text, sim_dino

def deepface_embed_folder(image_paths, model_name='ArcFace', detector_backend='retinaface', 
                          enforce_detection=True,
                          align=True, normalization="base"):
    """
    This function extracts faces from a list of images, and embeds them as embeddings. 

    Parameters:
            image_paths: exact image paths as a list of strings. numpy array (BGR) or based64 encoded
            images are also welcome. If one of pair has more than one face, then we will compare the
            face pair with max similarity.

            model_name (str): VGG-Face, Facenet, Facenet512, OpenFace, DeepFace, DeepID, Dlib
            , ArcFace and SFace

            enforce_detection (boolean): If no face could not be detected in an image, then this
            function will return exception by default. Set this to False not to have this exception.
            This might be convenient for low resolution images.

            detector_backend (string): set face detector backend to opencv, retinaface, mtcnn, ssd,
            dlib or mediapipe

    Returns:
            Returns a list of embeddings.

    """
    from deepface import DeepFace
    from deepface.commons import functions as deepface_functions

    # --------------------------------
    target_size = deepface_functions.find_target_size(model_name=model_name)
    all_embeddings = []

    for img_path in image_paths:
        embeddings = []

        try:
            # img_path might have many faces
            img_objs = deepface_functions.extract_faces(
                img=img_path,
                target_size=target_size,
                detector_backend=detector_backend,
                grayscale=False,
                enforce_detection=enforce_detection,
                align=align,
            )
        except:
            continue
        
        # now we will find the face pair with minimum distance
        for img_content, img_region, _ in img_objs:
            img_embedding_obj = DeepFace.represent(
                img_path=img_content,
                model_name=model_name,
                enforce_detection=enforce_detection,
                detector_backend="skip",
                align=align,
                normalization=normalization,
            )

            embedding = img_embedding_obj[0]["embedding"]
            embeddings.append(embedding)
        
        embeddings = np.array(embeddings)
        all_embeddings.append(embeddings)

    return all_embeddings

def insightface_embed_folder(insightface_app, image_paths, gpu_id=0, size=(512, 512)):
    """
    This function extracts faces from a list of images, and embeds them as embeddings. 

    Parameters:
            image_paths: exact image paths as a list of strings. numpy array (BGR) or based64 encoded
            images are also welcome. If one of pair has more than one face, then we will compare the
            face pair with max similarity.

            model_name (str): VGG-Face, Facenet, Facenet512, OpenFace, DeepFace, DeepID, Dlib
            , ArcFace and SFace

            enforce_detection (boolean): If no face could not be detected in an image, then this
            function will return exception by default. Set this to False not to have this exception.
            This might be convenient for low resolution images.

            detector_backend (string): set face detector backend to opencv, retinaface, mtcnn, ssd,
            dlib or mediapipe

    Returns:
            Returns a list of embeddings.

    """

    images_np = []
    for image_path in image_paths:
        image_np = np.array(Image.open(image_path))
        images_np.append(image_np)

    for i, image_np in enumerate(images_np):
        image_obj = Image.fromarray(image_np).resize((512, 512), Image.NEAREST)
        image_np = cv2.cvtColor(np.array(image_obj), cv2.COLOR_RGB2BGR)

    # Only for one-off call. Otherwise it will be very slow.
    if insightface_app is None:
        # FaceAnalysis will try to find the ckpt in: models/insightface/models/antelopev2. 
        # Note there's a second "model" in the path.        
        insightface_app = FaceAnalysis(name='antelopev2', root='models/insightface', providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])
        insightface_app.prepare(ctx_id=gpu_id, det_size=(512, 512))

    all_embeddings = []
    for idx, image in enumerate(images_np):
        # Resize image to (512, 512). The scheme is Image.NEAREST, to be consistent with 
        # PersonalizedBase dataset class.
        image = np.array(Image.fromarray(image).resize(size, Image.NEAREST))   
        face_infos = insightface_app.get(cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        if len(face_infos) > 0:
            face_info = sorted(face_infos, key=lambda x:(x['bbox'][2]-x['bbox'][0])*x['bbox'][3]-x['bbox'][1])[-1] # only use the maximum face
            # face_info.normed_embedding: [512,]. Already np.array, so no need to convert.
            all_embeddings.append([face_info.normed_embedding])
        else:
            # len(face_info) == 0 and skip_non_faces.
            # Skip images without faces.
            print(f'Skip image without face: {image_paths[idx]}')
            # Append an empty list to indicate that there is no face in this image.
            all_embeddings.append([])

    return all_embeddings

# src_embeds: N1 * embed_dim, dst_embeds: N2 * embed_dim
# return a similarity matrix of N1 * N2.
def np_cosine_similarity(src_embeds, dst_embeds):
    a = np.matmul(src_embeds, np.transpose(dst_embeds))
    b = np.sum(np.multiply(src_embeds, src_embeds), axis=1, keepdims=True)
    c = np.sum(np.multiply(dst_embeds, dst_embeds), axis=1, keepdims=True)
    return (a / (np.sqrt(b) * np.sqrt(c).T))
    
def calc_faces_mean_similarity(src_list_embeds, dst_list_embeds):
    """
    This function calculates similarity between two lists of face embeddings.

    Parameters:
            src_list_embeds: list of embeddings as numpy array
            dst_list_embeds: list of embeddings as numpy array

    Returns:
            Returns a list of similarity scores.
    """
    # --------------------------------
    # now we will find the face pair with minimum distance
    all_similarities = []
    src_no_face_img_count = 0
    dst_no_face_img_count = 0

    for src_embeds in src_list_embeds:
        if len(src_embeds) == 0:
            src_no_face_img_count += 1
    for dst_embeds in dst_list_embeds:
        if len(dst_embeds) == 0:
            dst_no_face_img_count += 1

    for src_embeds in src_list_embeds:
        if len(src_embeds) == 0:
            continue
        for dst_embeds in dst_list_embeds:
            if len(dst_embeds) == 0:
                continue
            # src_embeds: (num_faces1, embed_dim)
            # dst_embeds: (num_faces2, embed_dim)
            # sim_matrix: (num_faces1, num_faces2)
            sim_matrix = np_cosine_similarity(src_embeds, dst_embeds)
            max_sim = np.max(sim_matrix)
            all_similarities.append(max_sim)

    if len(all_similarities) == 0:
        mean_similarity = 0
    else:
        mean_similarity = np.mean(all_similarities)

    return mean_similarity, src_no_face_img_count, dst_no_face_img_count

# src_path, dst_path: a folder or a single image path
def compare_face_folders_fast(src_path, dst_path, src_num_samples=-1, dst_num_samples=-1, 
                              face_engine="deepface", insightface_app=None):

    img_extensions = [ "jpg", "jpeg", "png", "bmp" ]

    if os.path.isfile(src_path):
        src_paths = [ src_path ]
    else:
        src_paths = []
        for ext in img_extensions:
            src_paths += glob.glob(src_path + "/*" + ext)

    if os.path.isfile(dst_path):
        dst_paths = [ dst_path ]
    else:
        dst_paths = []
        for ext in img_extensions:
            dst_paths += glob.glob(dst_path + "/*" + ext)

    src_paths = sorted(src_paths)
    dst_paths = sorted(dst_paths)
    if src_num_samples > 0:
        src_paths = src_paths[-src_num_samples:]
    if dst_num_samples > 0:
        dst_paths = dst_paths[-dst_num_samples:]

    if   face_engine == "deepface":
        src_list_embeds = deepface_embed_folder(src_paths, model_name="ArcFace", detector_backend = "retinaface")
        dst_list_embeds = deepface_embed_folder(dst_paths, model_name="ArcFace", detector_backend = "retinaface")
    elif face_engine == "insightface":
        src_list_embeds = insightface_embed_folder(insightface_app, src_paths)
        dst_list_embeds = insightface_embed_folder(insightface_app, dst_paths)
    else:
        breakpoint()

    '''
    if face_engine == "deepface":
        (Pdb) calc_faces_mean_similarity(src_list_embeds, dst_list_embeds)
        (0.471041, 0, 0)
        (Pdb) calc_faces_mean_similarity(src_list_embeds, src_list_embeds)
        (0.622069, 0, 0)
        (Pdb) calc_faces_mean_similarity(dst_list_embeds, dst_list_embeds)
        (0.660250, 0, 0)
    if face_engine == "insightface":
        (Pdb) calc_faces_mean_similarity(src_list_embeds, dst_list_embeds)
        (0.339248, 0, 0)
        (Pdb) calc_faces_mean_similarity(src_list_embeds, src_list_embeds)
        (0.689450, 0, 0)
        (Pdb) calc_faces_mean_similarity(dst_list_embeds, dst_list_embeds)
        (0.480570, 0, 0)
    Seems that insightface embeddings are very sensitive to details like lightning, pose and tone.
    Therefore, by default we use deepface embeddings, as they only focus on face characteristics.
    '''

    avg_similarity, src_no_face_img_count, dst_no_face_img_count =\
        calc_faces_mean_similarity(src_list_embeds, dst_list_embeds)
    
    dst_normal_img_count = len(dst_paths) - dst_no_face_img_count

    if src_path[-1] == "/":
        src_path = src_path[:-1]
    if dst_path[-1] == "/":
        dst_path = dst_path[:-1]
    src_path_base = os.path.basename(src_path)
    dst_path_base = os.path.basename(dst_path)
    print("avg face sim: %.3f    '%s' vs '%s' (%d)" %(avg_similarity, src_path_base, dst_path_base, dst_no_face_img_count))
    return avg_similarity, dst_normal_img_count, dst_no_face_img_count

def compare_face_folders(src_path, dst_path, src_num_samples=-1, dst_num_samples=-1, verbose=False):
    from deepface import DeepFace

    if os.path.isfile(src_path):
        src_paths = [ src_path ]
    else:
        img_extensions = [ "jpg", "jpeg", "png", "bmp" ]
        src_paths = []
        for ext in img_extensions:
            src_paths += glob.glob(src_path + "/*" + ext)

    if os.path.isfile(dst_path):
        dst_paths = [ dst_path ]
    else:
        img_extensions = [ "jpg", "jpeg", "png", "bmp" ]
        dst_paths = []
        for ext in img_extensions:
            dst_paths += glob.glob(dst_path + "/*" + ext)

    src_paths = sorted(src_paths)
    dst_paths = sorted(dst_paths)
    if src_num_samples > 0:
        src_paths = src_paths[-src_num_samples:]
    if dst_num_samples > 0:
        dst_paths = dst_paths[-dst_num_samples:]

    normal_pair_count = 0
    except_pair_count = 0
    total_distance = 0
    total_pair_count = len(src_paths) * len(dst_paths)
    curr_pair_count = 0

    for src_img_path in src_paths:
        for dst_img_path in dst_paths:
            curr_pair_count += 1
            if src_img_path == dst_img_path:
                continue
            img1_name = os.path.basename(src_img_path)
            img2_name = os.path.basename(dst_img_path)
            if verbose:
                print("%d/%d: %s vs %s" %(curr_pair_count, total_pair_count, img1_name, img2_name))
            try:
                with torch.no_grad():
                    result = DeepFace.verify(img1_path = src_img_path, img2_path = dst_img_path, 
                                             model_name="ArcFace", detector_backend = "retinaface")
            except:
                except_pair_count += 1
                continue

            distance = result['distance']
            total_distance += distance
            normal_pair_count += 1
            curr_avg_distance = total_distance / normal_pair_count
            if verbose:
                print("%.3f / %.3f" %(distance, curr_avg_distance))

    if normal_pair_count > 0:
        avg_distance = total_distance / normal_pair_count
    else:
        avg_distance = 1

    avg_similarity = 1 - avg_distance
    print("Normal pairs: %d, exception pairs: %d" %(normal_pair_count, except_pair_count))
    if src_path[-1] == "/":
        src_path = src_path[:-1]
    if dst_path[-1] == "/":
        dst_path = dst_path[:-1]
    src_path_base = os.path.basename(src_path)
    dst_path_base = os.path.basename(dst_path)
    print("avg face sim: %.3f    '%s' vs '%s'" %(avg_similarity, src_path_base, dst_path_base))
    return avg_similarity, normal_pair_count, except_pair_count

# extra_sig could be a regular expression
def find_first_match(lst, search_term, extra_sig=""):
    for item in lst:
        if search_term in item and re.search(extra_sig, item):
            return item
    return None  # If no match is found

# if fix_1_offset: range_str "3-7,8,10" => [2, 3, 4, 5, 6, 7, 9]
# else:            range_str "3-7,8,10" => [3, 4, 5, 6, 7, 8, 10]
# "a-b" is always inclusive, i.e., "a-b" = [a, a+1, ..., b]
def parse_range_str(range_str, fix_1_offset=True):
    if range_str is None:
        return None
    
    result = []
    offset = 1 if fix_1_offset else 0

    for part in range_str.split(','):
        if '-' in part:
            a, b = part.split('-')
            a, b = int(a) - offset, int(b) - offset
            result.extend(list(range(a, b + 1)))
        else:
            a = int(part) - offset
            result.append(a)
    return result

# prompt_set_name: 'dreambench', 'community', 'all'.
def format_prompt_list(subject_string, z_prefix, z_suffix, background_string, 
                       class_token, class_long_token, 
                       broad_class, prompt_set_name='all', use_fp_trick=False):
    object_prompt_list = [
    # The space between "{0} {1}" is removed, so that prompts 
    # for ada/static-layerwise/ti could be generated by
    # providing an empty class_token. To generate prompts for DreamBooth, 
    # provide a class_token starting with a space.
    'a {0}{1}{2} in the jungle',                                       # 0
    'a {0}{1}{2} in the snow',
    'a {0}{1}{2} on the beach',
    'a {0}{1}{2} on a cobblestone street',
    'a {0}{1}{2} on top of pink fabric',
    'a {0}{1}{2} on top of a wooden floor',                            # 5
    'a {0}{1}{2} with a city in the background',
    'a {0}{1}{2} with a mountain in the background',
    'a {0}{1}{2} with a blue house in the background',
    'a {0}{1}{2} on top of a purple rug in a forest',
    'a {0}{1}{2} with a wheat field in the background',                # 10
    'a {0}{1}{2} with a tree and autumn leaves in the background',
    'a {0}{1}{2} with the Eiffel Tower in the background',
    'a {0}{1}{2} floating on top of water',
    'a {0}{1}{2} floating in an ocean of milk',
    'a {0}{1}{2} on top of green grass with sunflowers around it',     # 15
    'a {0}{1}{2} on top of a mirror',
    'a {0}{1}{2} on top of the sidewalk in a crowded street',
    'a {0}{1}{2} on top of a dirt road',
    'a {0}{1}{2} on top of a white rug',
    'a {0}red {1}{2}',                                                 # 20
    'a {0}purple {1}{2}',
    'a {0}shiny {1}{2}',
    'a {0}wet {1}{2}',
    'a {0}cube shaped {1}{2}'
    ]

    # Remove 'on top of a wooden floor' and 'on top of a purple rug in a forest'
    # They will lead to weird layouts.
    animal_prompt_list = [
    'a {0}{1}{2} in the jungle',                                       # 0
    'a {0}{1}{2} in the snow',
    'a {0}{1}{2} on the beach',
    'a {0}{1}{2} on a cobblestone street',
    'a {0}{1}{2} on top of pink fabric',
    'a {0}{1}{2} with a city in the background',                       # 5
    'a {0}{1}{2} with a mountain in the background',
    'a {0}{1}{2} with a blue house in the background',
    'a {0}{1}{2} wearing a red hat',                                   
    'a {0}{1}{2} wearing a santa hat',
    'a {0}{1}{2} wearing a rainbow scarf',                             # 10
    'a {0}{1}{2} wearing a black top hat and a monocle',
    'a {0}{1}{2} in a chef outfit',
    'a {0}{1}{2} in a firefighter outfit',                             
    'a {0}{1}{2} in a police outfit',
    'a {0}{1}{2} wearing pink glasses',                                # 15
    'a {0}{1}{2} wearing a yellow shirt',
    'a {0}{1}{2} in a purple wizard outfit',
    'a {0}red {1}{2}',                                                 
    'a {0}purple {1}{2}',
    'a {0}shiny {1}{2}',                                               # 20
    'a {0}wet {1}{2}',
    ]

    # humans/animals and cartoon characters.
    if broad_class == 1 or broad_class == 2:
        if prompt_set_name == 'community':
            orig_prompt_list = community_prompt_list
        elif prompt_set_name == 'dreambench':
            orig_prompt_list = animal_prompt_list
        elif prompt_set_name == 'all':
            orig_prompt_list = animal_prompt_list + community_prompt_list
        else:
            breakpoint()
    else:
        # object.
        orig_prompt_list = object_prompt_list

    z_suffix += background_string
    # z_prefix is often "portrait of" or "face portrait of".
    # If z_prefix is not empty and does not start with a space, append a space to separate with the subject.
    if len(z_prefix) > 0 and not z_prefix.endswith(" "):
        z_prefix = z_prefix + " "

    # Shift the content of z_prefix to the beginning of subject_string.
    subject_string = z_prefix + subject_string
    z_prefix = ""
    if use_fp_trick:
        # Prepend ", face portrait," to z_suffix to make sure the subject is always expressed.
        # There's a space after z_suffix in the prompt, so no need to add a space here.
        z_suffix = ", face portrait," + z_suffix
    
    if class_token in subject_string:
        class_token = ""
    if class_long_token in subject_string:
        class_long_token = ""

    prompt_list            = [ prompt.format(z_prefix, subject_string,   z_suffix)           for prompt in orig_prompt_list ]
    orig_short_prompt_list = [ prompt.format(z_prefix, class_token,      background_string)  for prompt in orig_prompt_list ]
    orig_long_prompt_list  = [ prompt.format(z_prefix, class_long_token, background_string)  for prompt in orig_prompt_list ]
    return prompt_list, orig_short_prompt_list, orig_long_prompt_list

# affix_name: z_prefix_type or z_suffix_type.
# use_fp_trick is only True if affix_name is z_prefix_type.
def reformat_z_affix(args, affix_name, broad_class, class_name, cls_delta_string):
    if not hasattr(args, affix_name):
        if broad_class == 1:
            # For ada/TI, if not human faces / animals, and z_affix_type is not specified, 
            # then use class_name as the z suffix, to make sure the subject is always expressed.
            z_affix_type = '' 
        else:
            z_affix_type = 'class_name'
    else:
        z_affix_type = getattr(args, affix_name)

    if z_affix_type == 'cls_delta_string':
        # DreamBooth always uses cls_delta_string as z_affix.
        z_affix = cls_delta_string
    elif z_affix_type == 'class_name':
        # For Ada/TI, if we append class token to "z" -> "z dog", 
        # the chance of occasional under-expression of the subject may be reduced.
        # (This trick is not needed for human faces)
        # Prepend a space to class_name to avoid "a zcat" -> "a z cat"
        z_affix = class_name
    else:
        # z_affix_type starts with a punctuation, e.g., ",".
        z_affix = z_affix_type

    # z_affix starts with a letter or number, instead of a punctuation, e.g., ",".
    if re.match(r"^[a-zA-Z0-9_]", z_affix):
        if affix_name == 'z_suffix_type':
            # If z_suffix_type starts with a word, prepend a space to avoid "a zcat" -> "a z cat"
            z_affix = " " + z_affix
        elif affix_name == 'z_prefix_type':
            # If z_prefix_type starts with a word, append a space to avoid "zcat" -> "z cat"
            z_affix = z_affix + " "
        else:
            breakpoint()
    # Otherwise, z_affix starts with a punctuation, e.g., ",".
    # If affix_name is z_prefix_type, append a space.
    elif affix_name == 'z_prefix_type':
        z_affix = z_affix + " "
    # Otherwise, z_affix starts with a punctuation, and affix_name is z_suffix_type. Do nothing.

    return z_affix
