"""把 damo (Modelscope) Chinese-CLIP 权重转换为 HuggingFace 格式"""
from pathlib import Path
import torch, re

damo_bin = Path(__file__).parent / "models_cache_ms/damo/multi-modal_clip-vit-base-patch16_zh/pytorch_model.bin"
flat_dir = Path(__file__).parent / "models_cache_ms/chinese-clip-vit-base-patch16"

raw = torch.load(str(damo_bin), map_location="cpu", weights_only=True)
state = raw["state_dict"]
new_state = {}

for key, tensor in state.items():
    k = key.removeprefix("module.")

    mapping = {
        "logit_scale": "logit_scale",
        "text_projection": "text_projection.weight",
        "visual.proj": "visual_projection.weight",
        "visual.class_embedding": "vision_model.embeddings.class_embedding",
        "visual.positional_embedding": "vision_model.embeddings.position_embedding.weight",
        "visual.conv1.weight": "vision_model.embeddings.patch_embedding.weight",
        "visual.ln_pre.weight": "vision_model.pre_layrnorm.weight",
        "visual.ln_pre.bias": "vision_model.pre_layrnorm.bias",
        "visual.ln_post.weight": "vision_model.post_layernorm.weight",
        "visual.ln_post.bias": "vision_model.post_layernorm.bias",
        "text.token_embedding.weight": "text_model.embeddings.word_embeddings.weight",
        "text.positional_embedding": "text_model.embeddings.position_embeddings.weight",
        "text.ln_final.weight": "text_model.embeddings.LayerNorm.weight",
        "text.ln_final.bias": "text_model.embeddings.LayerNorm.bias",
    }
    if k in mapping:
        new_state[mapping[k]] = tensor
        continue

    # Vision: fused qkv -> split
    m = re.match(r"visual\.transformer\.resblocks\.(\d+)\.attn\.in_proj_(weight|bias)", k)
    if m:
        n, attr = int(m.group(1)), m.group(2)
        q, kk, v = tensor.chunk(3, dim=0)
        new_state[f"vision_model.encoder.layers.{n}.self_attn.q_proj.{attr}"] = q
        new_state[f"vision_model.encoder.layers.{n}.self_attn.k_proj.{attr}"] = kk
        new_state[f"vision_model.encoder.layers.{n}.self_attn.v_proj.{attr}"] = v
        continue

    m = re.match(r"visual\.transformer\.resblocks\.(\d+)\.attn\.out_proj\.(\w+)", k)
    if m:
        n, attr = int(m.group(1)), m.group(2)
        new_state[f"vision_model.encoder.layers.{n}.self_attn.out_proj.{attr}"] = tensor
        continue

    m = re.match(r"visual\.transformer\.resblocks\.(\d+)\.ln_(\d)\.(\w+)", k)
    if m:
        n, ln_n, attr = int(m.group(1)), m.group(2), m.group(3)
        new_state[f"vision_model.encoder.layers.{n}.layer_norm{ln_n}.{attr}"] = tensor
        continue

    m = re.match(r"visual\.transformer\.resblocks\.(\d+)\.mlp\.c_(fc|proj)\.(\w+)", k)
    if m:
        n, mlp_type, attr = int(m.group(1)), m.group(2), m.group(3)
        target = "fc1" if mlp_type == "fc" else "fc2"
        new_state[f"vision_model.encoder.layers.{n}.mlp.{target}.{attr}"] = tensor
        continue

    # Text: fused qkv
    m = re.match(r"text\.transformer\.resblocks\.(\d+)\.attn\.in_proj_(weight|bias)", k)
    if m:
        n, attr = int(m.group(1)), m.group(2)
        q, kk, v = tensor.chunk(3, dim=0)
        new_state[f"text_model.encoder.layer.{n}.attention.self.query.{attr}"] = q
        new_state[f"text_model.encoder.layer.{n}.attention.self.key.{attr}"] = kk
        new_state[f"text_model.encoder.layer.{n}.attention.self.value.{attr}"] = v
        continue

    m = re.match(r"text\.transformer\.resblocks\.(\d+)\.attn\.out_proj\.(\w+)", k)
    if m:
        n, attr = int(m.group(1)), m.group(2)
        new_state[f"text_model.encoder.layer.{n}.attention.output.dense.{attr}"] = tensor
        continue

    m = re.match(r"text\.transformer\.resblocks\.(\d+)\.ln_1\.(\w+)", k)
    if m:
        n, attr = int(m.group(1)), m.group(2)
        new_state[f"text_model.encoder.layer.{n}.attention.output.LayerNorm.{attr}"] = tensor
        continue

    m = re.match(r"text\.transformer\.resblocks\.(\d+)\.ln_2\.(\w+)", k)
    if m:
        n, attr = int(m.group(1)), m.group(2)
        new_state[f"text_model.encoder.layer.{n}.output.LayerNorm.{attr}"] = tensor
        continue

    m = re.match(r"text\.transformer\.resblocks\.(\d+)\.mlp\.c_fc\.(\w+)", k)
    if m:
        n, attr = int(m.group(1)), m.group(2)
        new_state[f"text_model.encoder.layer.{n}.intermediate.dense.{attr}"] = tensor
        continue

    m = re.match(r"text\.transformer\.resblocks\.(\d+)\.mlp\.c_proj\.(\w+)", k)
    if m:
        n, attr = int(m.group(1)), m.group(2)
        new_state[f"text_model.encoder.layer.{n}.output.dense.{attr}"] = tensor
        continue

    # bert.* text encoder -> text_model.* (HF ChineseCLIPModel BERT-style naming)
    if k.startswith("bert."):
        new_state["text_model." + k[5:]] = tensor
        continue

    print(f"UNMAPPED: {k}")

# damo projection 矩阵行列反了，需要转置
for proj_key in ("text_projection.weight", "visual_projection.weight"):
    if proj_key in new_state and new_state[proj_key].shape[0] != 512:
        new_state[proj_key] = new_state[proj_key].T.contiguous()
        print(f"转置 {proj_key} -> {list(new_state[proj_key].shape)}")

print(f"转换: {len(state)} -> {len(new_state)} keys")

# 备份原 symlink
out = flat_dir / "pytorch_model.bin"
bak = flat_dir / "pytorch_model.bin.damo"
if out.is_symlink():
    out.rename(bak)
    print(f"symlink 备份 -> {bak}")

torch.save(new_state, str(out))
print(f"✅ 写入 {out} ({out.stat().st_size/1024/1024:.1f} MB)")
