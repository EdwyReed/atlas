"""Engine-owned image-mode graph recipes, extracted from invoke CLI. No subprocess or imports of old backends."""
import uuid

def ident(model: dict) -> dict:
    return {
        "key": model["key"],
        "hash": model["hash"],
        "name": model["name"],
        "base": model["base"],
        "type": model["type"],
    }

def edge(src: str, src_field: str, dst: str, dst_field: str) -> dict:
    return {
        "source": {"node_id": src, "field": src_field},
        "destination": {"node_id": dst, "field": dst_field},
    }

def apply_board(node: dict, board_id: str | None) -> None:
    if board_id:
        node["board"] = {"board_id": board_id}

def attach_metadata(
    nodes,
    edges,
    *,
    generation_mode: str,
    prompt: str,
    negative: str,
    width: int,
    height: int,
    seed: int,
    cfg: float,
    steps: int,
    scheduler: str,
    model: dict,
    loras,
    clip_skip: int | None = None,
    vae=None,
    qwen3=None,
    strength: float | None = None,
    init_image: str | None = None,
) -> None:
    meta = {
        "id": "meta",
        "type": "core_metadata",
        "generation_mode": generation_mode,
        "positive_prompt": prompt,
        "negative_prompt": negative,
        "width": width,
        "height": height,
        "seed": seed,
        "rand_device": "cpu",
        "cfg_scale": cfg,
        "steps": steps,
        "scheduler": scheduler,
        "model": ident(model),
        "is_intermediate": True,
        "use_cache": False,
    }
    if clip_skip:
        meta["clip_skip"] = clip_skip
    packed = [{"model": ident(m), "weight": w} for m, w in (loras or [])]
    if packed:
        meta["loras"] = packed
    if vae is not None:
        meta["vae"] = ident(vae)
    if qwen3 is not None:
        meta["qwen3_encoder"] = ident(qwen3)
    if init_image:
        meta["init_image"] = init_image
        if strength is not None:
            meta["strength"] = strength
    nodes["meta"] = meta
    edges.append(edge("meta", "metadata", "l2i", "metadata"))

def wire_inpaint(nodes, edges, *, init_name: str, mask_name: str, i2l_type: str, strength: float, tiled: bool) -> None:
    """Wire img2latent + denoise mask. White mask = edit region.

    SD1.5/SDXL: create_denoise_mask (needs SD/SDXL VAE).
    Anima: create_gradient_mask without VAE — create_denoise_mask rejects Anima VAE
    ('VAE must be of type SD-1.5 or SDXL').
    """
    nodes["init"] = {
        "id": "init",
        "type": "image",
        "image": {"image_name": init_name},
        "is_intermediate": True,
        "use_cache": True,
    }
    nodes["mask"] = {
        "id": "mask",
        "type": "image",
        "image": {"image_name": mask_name},
        "is_intermediate": True,
        "use_cache": True,
    }
    nodes["i2l"] = {
        "id": "i2l",
        "type": i2l_type,
        "is_intermediate": True,
        "use_cache": False,
    }
    if i2l_type == "i2l":
        nodes["i2l"]["tiled"] = tiled
    start = max(0.0, min(1.0, 1.0 - float(strength)))
    nodes["denoise"]["denoising_start"] = start
    edges += [
        edge("init", "image", "i2l", "image"),
        edge("loader", "vae", "i2l", "vae"),
        edge("i2l", "latents", "denoise", "latents"),
    ]
    if i2l_type == "anima_i2l":
        # No VAE on mask node — Anima VAE is not accepted by create_denoise_mask.
        nodes["inpaint_mask"] = {
            "id": "inpaint_mask",
            "type": "create_gradient_mask",
            "edge_radius": 12,
            "coherence_mode": "Gaussian Blur",
            "minimum_denoise": 0.0,
            "tiled": False,
            "fp32": False,
            "is_intermediate": True,
            "use_cache": False,
        }
        edges += [
            edge("mask", "image", "inpaint_mask", "mask"),
            edge("inpaint_mask", "denoise_mask", "denoise", "denoise_mask"),
        ]
    else:
        nodes["inpaint_mask"] = {
            "id": "inpaint_mask",
            "type": "create_denoise_mask",
            "tiled": tiled,
            "is_intermediate": True,
            "use_cache": False,
        }
        edges += [
            edge("init", "image", "inpaint_mask", "image"),
            edge("mask", "image", "inpaint_mask", "mask"),
            edge("loader", "vae", "inpaint_mask", "vae"),
            edge("inpaint_mask", "denoise_mask", "denoise", "denoise_mask"),
        ]

def sd1_graph(model, prompt, negative, *, width, height, steps, cfg, seed, scheduler, clip_skip, loras, board_id=None, init_name=None, mask_name=None, strength=0.75) -> dict:
    nodes = {}
    edges = []
    nodes["loader"] = {
        "id": "loader",
        "type": "main_model_loader",
        "model": ident(model),
        "is_intermediate": True,
        "use_cache": True,
    }
    unet_src, clip_src = "loader", "loader"
    unet_field, clip_field = "unet", "clip"
    for i, (lora_model, weight) in enumerate(loras):
        nid = f"lora_{i}"
        nodes[nid] = {
            "id": nid,
            "type": "lora_loader",
            "lora": ident(lora_model),
            "weight": weight,
            "is_intermediate": True,
            "use_cache": True,
        }
        edges += [
            edge(unet_src, unet_field, nid, "unet"),
            edge(clip_src, clip_field, nid, "clip"),
        ]
        unet_src, clip_src, unet_field, clip_field = nid, nid, "unet", "clip"
    if clip_skip:
        nodes["clip_skip"] = {
            "id": "clip_skip",
            "type": "clip_skip",
            "skipped_layers": clip_skip,
            "is_intermediate": True,
            "use_cache": True,
        }
        edges.append(edge(clip_src, clip_field, "clip_skip", "clip"))
        clip_src, clip_field = "clip_skip", "clip"
    for nid, text in (("pos", prompt), ("neg", negative)):
        nodes[nid] = {
            "id": nid,
            "type": "compel",
            "prompt": text,
            "is_intermediate": True,
            "use_cache": True,
        }
        edges.append(edge(clip_src, clip_field, nid, "clip"))
    nodes["noise"] = {
        "id": "noise",
        "type": "noise",
        "noise_type": "SD",
        "seed": seed,
        "width": width,
        "height": height,
        "use_cpu": True,
        "is_intermediate": True,
        "use_cache": False,
    }
    nodes["denoise"] = {
        "id": "denoise",
        "type": "denoise_latents",
        "steps": steps,
        "cfg_scale": cfg,
        "scheduler": scheduler,
        "denoising_start": 0.0,
        "denoising_end": 1.0,
        "is_intermediate": True,
        "use_cache": False,
    }
    nodes["l2i"] = {
        "id": "l2i",
        "type": "l2i",
        "is_intermediate": False,
        "use_cache": False,
        "tiled": True,
    }
    apply_board(nodes["l2i"], board_id)
    edges += [
        edge(unet_src, unet_field, "denoise", "unet"),
        edge("pos", "conditioning", "denoise", "positive_conditioning"),
        edge("neg", "conditioning", "denoise", "negative_conditioning"),
        edge("noise", "noise", "denoise", "noise"),
        edge("loader", "vae", "l2i", "vae"),
        edge("denoise", "latents", "l2i", "latents"),
    ]
    if init_name and mask_name:
        wire_inpaint(
            nodes,
            edges,
            init_name=init_name,
            mask_name=mask_name,
            i2l_type="i2l",
            strength=strength,
            tiled=True,
        )
    attach_metadata(
        nodes,
        edges,
        generation_mode="inpaint" if (init_name and mask_name) else "txt2img",
        prompt=prompt,
        negative=negative,
        width=width,
        height=height,
        seed=seed,
        cfg=cfg,
        steps=steps,
        scheduler=scheduler,
        model=model,
        loras=loras,
        clip_skip=clip_skip,
        strength=strength if (init_name and mask_name) else None,
        init_image=init_name,
    )
    return {"id": str(uuid.uuid4()), "nodes": nodes, "edges": edges}

def anima_graph(model, vae, encoder, prompt, negative, *, width, height, steps, cfg, seed, scheduler, loras, board_id=None, init_name=None, mask_name=None, strength=0.75) -> dict:
    nodes = {}
    edges = []
    nodes["loader"] = {
        "id": "loader",
        "type": "anima_model_loader",
        "model": ident(model),
        "vae_model": ident(vae),
        "qwen3_encoder_model": ident(encoder),
        "is_intermediate": True,
        "use_cache": True,
    }
    tf_src, enc_src = "loader", "loader"
    tf_field, enc_field = "transformer", "qwen3_encoder"
    for i, (lora_model, weight) in enumerate(loras):
        nid = f"lora_{i}"
        nodes[nid] = {
            "id": nid,
            "type": "anima_lora_loader",
            "lora": ident(lora_model),
            "weight": weight,
            "is_intermediate": True,
            "use_cache": True,
        }
        edges += [
            edge(tf_src, tf_field, nid, "transformer"),
            edge(enc_src, enc_field, nid, "qwen3_encoder"),
        ]
        tf_src, enc_src, tf_field, enc_field = nid, nid, "transformer", "qwen3_encoder"
    nodes["pos"] = {
        "id": "pos",
        "type": "anima_text_encoder",
        "prompt": prompt,
        "is_intermediate": True,
        "use_cache": True,
    }
    edges.append(edge(enc_src, enc_field, "pos", "qwen3_encoder"))
    nodes["denoise"] = {
        "id": "denoise",
        "type": "anima_denoise",
        "guidance_scale": cfg,
        "steps": steps,
        "seed": seed,
        "width": width,
        "height": height,
        "scheduler": scheduler,
        "denoising_start": 0.0,
        "denoising_end": 1.0,
        "is_intermediate": True,
        "use_cache": False,
    }
    edges += [
        edge(tf_src, tf_field, "denoise", "transformer"),
        edge("pos", "conditioning", "denoise", "positive_conditioning"),
    ]
    if cfg > 1:
        nodes["neg"] = {
            "id": "neg",
            "type": "anima_text_encoder",
            "prompt": negative,
            "is_intermediate": True,
            "use_cache": True,
        }
        edges += [
            edge(enc_src, enc_field, "neg", "qwen3_encoder"),
            edge("neg", "conditioning", "denoise", "negative_conditioning"),
        ]
    nodes["l2i"] = {
        "id": "l2i",
        "type": "anima_l2i",
        "is_intermediate": False,
        "use_cache": False,
    }
    apply_board(nodes["l2i"], board_id)
    edges += [
        edge("loader", "vae", "l2i", "vae"),
        edge("denoise", "latents", "l2i", "latents"),
    ]
    if init_name and mask_name:
        wire_inpaint(
            nodes,
            edges,
            init_name=init_name,
            mask_name=mask_name,
            i2l_type="anima_i2l",
            strength=strength,
            tiled=False,
        )
    attach_metadata(
        nodes,
        edges,
        generation_mode="anima_inpaint" if (init_name and mask_name) else "anima_txt2img",
        prompt=prompt,
        negative=negative,
        width=width,
        height=height,
        seed=seed,
        cfg=cfg,
        steps=steps,
        scheduler=scheduler,
        model=model,
        loras=loras,
        vae=vae,
        qwen3=encoder,
        strength=strength if (init_name and mask_name) else None,
        init_image=init_name,
    )
    return {"id": str(uuid.uuid4()), "nodes": nodes, "edges": edges}

def upscale_graph(*, image_name: str, engine: str, spandrel_model, esrgan_name: str, scale: float, tile: int, board_id: str | None) -> dict:
    nodes = {
        "init": {
            "id": "init",
            "type": "image",
            "image": {"image_name": image_name},
            "is_intermediate": True,
            "use_cache": True,
        }
    }
    edges = []
    if engine == "esrgan":
        nodes["up"] = {
            "id": "up",
            "type": "esrgan",
            "model_name": esrgan_name,
            "tile_size": tile,
            "is_intermediate": False,
            "use_cache": False,
        }
    elif abs(float(scale) - 2.0) < 1e-6 and spandrel_model and "x2" in (spandrel_model.get("name") or "").lower():
        nodes["up"] = {
            "id": "up",
            "type": "spandrel_image_to_image",
            "image_to_image_model": ident(spandrel_model),
            "tile_size": tile,
            "is_intermediate": False,
            "use_cache": False,
        }
    else:
        nodes["up"] = {
            "id": "up",
            "type": "spandrel_image_to_image_autoscale",
            "image_to_image_model": ident(spandrel_model),
            "tile_size": tile,
            "scale": float(scale),
            "fit_to_multiple_of_8": True,
            "is_intermediate": False,
            "use_cache": False,
        }
    apply_board(nodes["up"], board_id)
    edges.append(edge("init", "image", "up", "image"))
    return {"id": str(uuid.uuid4()), "nodes": nodes, "edges": edges}
