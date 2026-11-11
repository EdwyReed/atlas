"""Engine-owned graph recipes extracted from invokeai-native 0.6.5. No plugin imports.
Original source sha256: 16c84c4dc6ba27653789301f038f0a8db84bd3f53eed640a2fa7eeb4d6e9b54e
"""
import uuid
from urllib.parse import urlsplit,urlunsplit
from .contracts import Fault as InvokeError

def _model_ref(m):
 source=str(m.get("source") or "")
 if source.startswith(("http://","https://")):
  parts=urlsplit(source); source=urlunsplit((parts.scheme,parts.netloc,parts.path,"",""))
 return {**{k:m.get(k) for k in ("key","hash","name","base","type","path")}, **({"source":source} if source else {})}

def _model_type(value):
 # “checkpoint” is the user-facing InvokeAI term for the main generation model.
 # The REST registry calls the same type “main”.
 return "main" if str(value or "").lower()=="checkpoint" else value

def _selector(value):
 if isinstance(value,dict): value=value.get("key") or value.get("name") or value.get("model")
 value=str(value or "").strip()
 if not value: raise InvokeError("VALIDATION","model selector is required (use an exact catalog.list name or key)")
 return value

def _find(models, wanted, wanted_type=None):
 wanted=_selector(wanted); wanted_type=_model_type(wanted_type)
 matches=[m for m in models if (not wanted_type or m.get("type")==wanted_type) and wanted.lower() in {str(m.get("key")).lower(),str(m.get("name")).lower()}]
 if not matches: matches=[m for m in models if (not wanted_type or m.get("type")==wanted_type) and wanted.lower() in str(m.get("name","")).lower()]
 if not matches: raise InvokeError("NOT_FOUND",f"model not found: {wanted}")
 if len(matches)>1: raise InvokeError("AMBIGUOUS_MODEL",f"ambiguous model: {wanted}; candidates: "+", ".join(str(x.get("name")) for x in matches[:8]))
 return matches[0]

def _edge(src, field, dst, dst_field):
 return {"source":{"node_id":src,"field":field},"destination":{"node_id":dst,"field":dst_field}}

def _metadata(nodes, edges, out, model, prompt, negative, seed, width, height, steps, cfg, scheduler, mode, loras, **extra):
 ident=_model_ref(model)
 nodes["meta"]={"id":"meta","type":"core_metadata","generation_mode":mode,"positive_prompt":prompt,"negative_prompt":negative or "","width":width,"height":height,"seed":seed,"rand_device":"cpu","steps":steps,"cfg_scale":cfg,"scheduler":scheduler,"model":ident,"is_intermediate":True,"use_cache":False}
 if loras: nodes["meta"]["loras"]=[{"model":_model_ref(x),"weight":x["weight"]} for x in loras]
 for k,v in extra.items():
  if v is not None: nodes["meta"][k]=_model_ref(v) if isinstance(v,dict) else v
 edges.append(_edge("meta","metadata",out,"metadata"))

def _sd1_graph(model,prompt,negative,seed,width,height,steps,cfg,scheduler,loras):
 ident=_model_ref(model); nodes={"model":{"id":"model","type":"main_model_loader","model":ident,"is_intermediate":True,"use_cache":True},"clip":{"id":"clip","type":"clip_skip","skipped_layers":2,"is_intermediate":True,"use_cache":True},"pos":{"id":"pos","type":"compel","prompt":prompt,"is_intermediate":True,"use_cache":True},"neg":{"id":"neg","type":"compel","prompt":negative or "","is_intermediate":True,"use_cache":True},"noise":{"id":"noise","type":"noise","noise_type":"SD","seed":seed,"width":width,"height":height,"use_cpu":True,"is_intermediate":True,"use_cache":False},"denoise":{"id":"denoise","type":"denoise_latents","steps":steps,"cfg_scale":cfg,"scheduler":scheduler,"denoising_start":0.0,"denoising_end":1.0,"is_intermediate":True,"use_cache":False},"out":{"id":"out","type":"l2i","is_intermediate":False,"use_cache":False,"tiled":True}}
 edges=[]; unet_src=clip_src="model"
 for i,lora in enumerate(loras):
  nid=f"lora_{i}"; nodes[nid]={"id":nid,"type":"lora_loader","lora":_model_ref(lora),"weight":lora["weight"],"is_intermediate":True,"use_cache":True}
  edges += [_edge(unet_src,"unet",nid,"unet"),_edge(clip_src,"clip",nid,"clip")]; unet_src=clip_src=nid
 edges += [_edge(clip_src,"clip","clip","clip"),_edge("clip","clip","pos","clip"),_edge("clip","clip","neg","clip"),_edge(unet_src,"unet","denoise","unet"),_edge("pos","conditioning","denoise","positive_conditioning"),_edge("neg","conditioning","denoise","negative_conditioning"),_edge("noise","noise","denoise","noise"),_edge("model","vae","out","vae"),_edge("denoise","latents","out","latents")]
 _metadata(nodes,edges,"out",model,prompt,negative,seed,width,height,steps,cfg,scheduler,"txt2img",loras,clip_skip=2)
 return {"id":str(uuid.uuid4()),"nodes":nodes,"edges":edges}

def _sdxl_graph(model,prompt,negative,seed,width,height,steps,cfg,scheduler,loras):
 nodes={"model":{"id":"model","type":"sdxl_model_loader","model":_model_ref(model),"is_intermediate":True,"use_cache":True},"pos":{"id":"pos","type":"sdxl_compel_prompt","prompt":prompt,"style":"","original_width":width,"original_height":height,"target_width":width,"target_height":height,"crop_top":0,"crop_left":0,"is_intermediate":True,"use_cache":True},"neg":{"id":"neg","type":"sdxl_compel_prompt","prompt":negative or "","style":"","original_width":width,"original_height":height,"target_width":width,"target_height":height,"crop_top":0,"crop_left":0,"is_intermediate":True,"use_cache":True},"noise":{"id":"noise","type":"noise","noise_type":"SD","seed":seed,"width":width,"height":height,"use_cpu":True,"is_intermediate":True,"use_cache":False},"denoise":{"id":"denoise","type":"denoise_latents","steps":steps,"cfg_scale":cfg,"scheduler":scheduler,"denoising_start":0.0,"denoising_end":1.0,"is_intermediate":True,"use_cache":False},"out":{"id":"out","type":"l2i","is_intermediate":False,"use_cache":False,"tiled":True}}
 edges=[]; unet=clip=clip2="model"
 for i,lora in enumerate(loras):
  nid=f"lora_{i}"; nodes[nid]={"id":nid,"type":"sdxl_lora_loader","lora":_model_ref(lora),"weight":lora["weight"],"is_intermediate":True,"use_cache":True}; edges += [_edge(unet,"unet",nid,"unet"),_edge(clip,"clip",nid,"clip"),_edge(clip2,"clip2",nid,"clip2")]; unet=clip=clip2=nid
 edges += [_edge(clip,"clip","pos","clip"),_edge(clip2,"clip2","pos","clip2"),_edge(clip,"clip","neg","clip"),_edge(clip2,"clip2","neg","clip2"),_edge(unet,"unet","denoise","unet"),_edge("pos","conditioning","denoise","positive_conditioning"),_edge("neg","conditioning","denoise","negative_conditioning"),_edge("noise","noise","denoise","noise"),_edge("model","vae","out","vae"),_edge("denoise","latents","out","latents")]
 _metadata(nodes,edges,"out",model,prompt,negative,seed,width,height,steps,cfg,scheduler,"txt2img",loras)
 return {"id":str(uuid.uuid4()),"nodes":nodes,"edges":edges}

def _component(models,cfg,base,key,model_type,default):
 spec=((cfg.get("families") or {}).get(base) or {}).get(key) or cfg.get(key) or default
 return _find(models,spec,model_type)

def _transformer_graph(family, model, prompt, negative, seed, width, height, steps, cfg_scale, scheduler, loras, deps):
 # InvokeAI exposes dedicated nodes per transformer family; this shared builder routes by catalog base, never by model-name heuristics.
 f={"anima":("anima_model_loader","anima_lora_loader","anima_text_encoder","anima_denoise","anima_l2i","qwen3_encoder","guidance_scale","steps","positive_conditioning","negative_conditioning"),"z-image":("z_image_model_loader","z_image_lora_loader","z_image_text_encoder","z_image_denoise","z_image_l2i","qwen3_encoder","guidance_scale","steps","positive_conditioning","negative_conditioning"),"flux":("flux_model_loader","flux_lora_loader","flux_text_encoder","flux_denoise","flux_vae_decode","t5_encoder","guidance","num_steps","positive_text_conditioning","negative_text_conditioning")}[family]
 loader,lora_type,text_type,denoise_type,out_type,encoder_field,guidance_field,steps_field,pos_field,neg_field=f
 loader_node={"id":"model","type":loader,"model":_model_ref(model),"is_intermediate":True,"use_cache":True}
 for key,value in deps.items(): loader_node[key]=_model_ref(value)
 nodes={"model":loader_node,"pos":{"id":"pos","type":text_type,"prompt":prompt,"is_intermediate":True,"use_cache":True},"denoise":{"id":"denoise","type":denoise_type,guidance_field:cfg_scale,steps_field:steps,"seed":seed,"width":width,"height":height,"scheduler":scheduler,"denoising_start":0.0,"denoising_end":1.0,"is_intermediate":True,"use_cache":False},"out":{"id":"out","type":out_type,"is_intermediate":False,"use_cache":False}}
 edges=[]; transformer=encoder="model"; aux=[]
 if family=="flux": aux=[("clip","clip"),("t5_encoder","t5_encoder")]
 for i,lora in enumerate(loras):
  nid=f"lora_{i}"; nodes[nid]={"id":nid,"type":lora_type,"lora":_model_ref(lora),"weight":lora["weight"],"is_intermediate":True,"use_cache":True}; edges.append(_edge(transformer,"transformer",nid,"transformer")); edges.append(_edge(encoder,encoder_field,nid,encoder_field))
  for field,_ in aux: edges.append(_edge(transformer,field,nid,field))
  transformer=encoder=nid
 if family=="flux":
  edges += [_edge(encoder,"clip","pos","clip"),_edge(encoder,"t5_encoder","pos","t5_encoder")]
 else: edges.append(_edge(encoder,encoder_field,"pos",encoder_field))
 edges += [_edge(transformer,"transformer","denoise","transformer"),_edge("pos","conditioning","denoise",pos_field),_edge("model","vae","out","vae"),_edge("denoise","latents","out","latents")]
 if cfg_scale>1 or family=="flux":
  nodes["neg"]={"id":"neg","type":text_type,"prompt":negative or "","is_intermediate":True,"use_cache":True}
  if family=="flux": edges += [_edge(encoder,"clip","neg","clip"),_edge(encoder,"t5_encoder","neg","t5_encoder")]
  else: edges.append(_edge(encoder,encoder_field,"neg",encoder_field))
  edges.append(_edge("neg","conditioning","denoise",neg_field))
 _metadata(nodes,edges,"out",model,prompt,negative,seed,width,height,steps,cfg_scale,scheduler,f"{family}_txt2img",loras,**deps)
 return {"id":str(uuid.uuid4()),"nodes":nodes,"edges":edges}

def _family_key(base):
 value=str(base or "").lower().replace("_","-")
 return {"sdxl":"sd-xl","sd-xl":"sd-xl","flux1":"flux","zimage":"z-image","z-image":"z-image"}.get(value,value)

def _build_graph(models,cfg,model,prompt,negative,seed,width,height,steps,cfg_scale,scheduler,loras):
 base=_family_key(model.get("base"))
 if base=="sd-1": return _sd1_graph(model,prompt,negative,seed,width,height,steps,cfg_scale,scheduler,loras)
 if base=="sd-xl": return _sdxl_graph(model,prompt,negative,seed,width,height,steps,cfg_scale,scheduler,loras)
 if base=="anima":
  deps={"vae_model":_component(models,cfg,base,"vae_model","vae","Anima QwenImage VAE"),"qwen3_encoder_model":_component(models,cfg,base,"qwen3_encoder_model","qwen3_encoder","Anima Qwen3 0.6B Text Encoder")}
  return _transformer_graph(base,model,prompt,negative,seed,width,height,steps,cfg_scale,scheduler,loras,deps)
 if base=="z-image":
  deps={"vae_model":_component(models,cfg,base,"vae_model","vae","Z-Image VAE"),"qwen3_encoder_model":_component(models,cfg,base,"qwen3_encoder_model","qwen3_encoder","Z-Image Text Encoder")}
  return _transformer_graph(base,model,prompt,negative,seed,width,height,steps,cfg_scale,scheduler,loras,deps)
 if base=="flux": return _transformer_graph(base,model,prompt,negative,seed,width,height,steps,cfg_scale,scheduler,loras,{})
 raise InvokeError("UNSUPPORTED_MODEL_FAMILY",f"no InvokeAI graph adapter is registered for model base {model.get('base')}; install or configure a family adapter")
