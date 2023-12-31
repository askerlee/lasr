import torch
from evaluation.eval_utils import find_first_match
import sys
import os
import re
import numpy as np

np.set_printoptions(precision=4, suppress=True)

ckpt_sig = sys.argv[1]
if len(sys.argv) > 2:
    extra_sig = sys.argv[2]
else:
    extra_sig = ""

all_ckpt_names = os.listdir("logs")
# Sort all_ckpt_names by name (actually by timestamp in the name), so that most recent first.
all_ckpt_names.sort(reverse=True)
ckpt_name  = find_first_match(all_ckpt_names, ckpt_sig, extra_sig=extra_sig)
# embeddings_gs-{ckpt_iter}.pt
emb_folder    = f"logs/{ckpt_name}/checkpoints/"
iter2path = {}
print("emb_folder:", emb_folder)

for emb_path in os.listdir(emb_folder):
    if re.match(r"embeddings_gs-(\d+).pt", emb_path):
        ckpt_iter = re.match(r"embeddings_gs-(\d+).pt", emb_path).group(1)
        ckpt_iter = int(ckpt_iter)
        #if ckpt_iter % 500 == 0:
        iter2path[ckpt_iter] = emb_path

iterations = sorted(iter2path.keys())

emb_path = os.path.join(emb_folder, iter2path[iterations[0]])
emb_ckpt = torch.load(emb_path)
tokens = emb_ckpt['string_to_emb_ema_dict'].keys()


print("emb_global_scale_scores:")
for idx, iteration in enumerate(iterations):
    if iteration % 100 != 0:
        continue
            
    emb_path = os.path.join(emb_folder, iter2path[iteration])
    emb_ckpt = torch.load(emb_path)

    print(f"{iteration}: {emb_ckpt['emb_global_scale_scores'].sigmoid() + 0.5}")

print()
    
for k in tokens:
    print(f"Token: {k}")

    print("Attn Poolers:")

    for idx, iteration in enumerate(iterations):
        if iteration % 100 != 0:
            continue

        emb_path = os.path.join(emb_folder, iter2path[iteration])
        emb_ckpt = torch.load(emb_path)

        ada_embedder = emb_ckpt['string_to_ada_embedder'][k]
        attn_poolers = ada_embedder.poolers
        for i, attn_pooler in enumerate(attn_poolers):
            lora_to_fg_q_mean = attn_pooler.lora_to_fg_q.weight.abs().mean()
            lora_to_bg_q_mean = attn_pooler.lora_to_bg_q.weight.abs().mean()
            lora_to_k_mean = attn_pooler.lora_to_k.weight.abs().mean()

            print(f"{iteration}-{i}: lora_to_fg_q: {lora_to_fg_q_mean:.4f}, lora_to_bg_q: {lora_to_bg_q_mean:.4f}, lora_to_k: {lora_to_k_mean:.4f}")

    print("layer_coeff_maps weight:")

    for idx, iteration in enumerate(iterations):
        if iteration % 100 != 0:
            continue
                
        emb_path = os.path.join(emb_folder, iter2path[iteration])
        emb_ckpt = torch.load(emb_path)

        ada_embedder = emb_ckpt['string_to_ada_embedder'][k]
        attn_poolers = ada_embedder.poolers

        layer_coeff_map_means = [ layer_coeff_map.weight.abs().mean().item() for layer_coeff_map in ada_embedder.layer_coeff_maps ]
        layer_coeff_map_means = np.array(layer_coeff_map_means)

        print(f"{iteration}: {layer_coeff_map_means}")

    print("layer_coeff_maps bias:")

    for idx, iteration in enumerate(iterations):
        if iteration % 100 != 0:
            continue
                
        emb_path = os.path.join(emb_folder, iter2path[iteration])
        emb_ckpt = torch.load(emb_path)

        ada_embedder = emb_ckpt['string_to_ada_embedder'][k]
        attn_poolers = ada_embedder.poolers

        layer_coeff_map_means = [ layer_coeff_map.bias.abs().mean().item() for layer_coeff_map in ada_embedder.layer_coeff_maps ]
        layer_coeff_map_means = np.array(layer_coeff_map_means)

        print(f"{iteration}: {layer_coeff_map_means}")
