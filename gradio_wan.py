import gradio as gr
import time
import requests
import json
import uuid
import os
import shutil
import tempfile
import random
from PIL import Image 

# --- ComfyUI Server Configuration ---
SERVER_LIPSYNC = "127.0.0.1:8222"        # Port 8222: Lipsync Only
SERVER_CHARACTER_V2V = "127.0.0.1:8224"  # Port 8224: Character/V2V/Upscale
SERVER_GEN_EDIT = "127.0.0.1:8223"       # Port 8223: T2V/Editing

WORKFLOWS = {} 

# --- 1. Central ComfyUI Workflow Runner (POLLING MODE) ---
def run_comfyui_workflow(server_address, workflow_name, inputs_map, files_map, output_node_id=None):
    """
    Executes a workflow and YIELDS updates using POLLING.
    Includes explicit fixes for LoadVideo/LoadImage caching issues.
    Prioritizes VIDEO outputs over images.
    """
    client_id = str(uuid.uuid4())
    base_url = f"http://{server_address}"
    headers = {'Connection': 'close'}

    if workflow_name not in WORKFLOWS:
        yield None, f"Error: Workflow '{workflow_name}' not found. Check 'workflows' folder."
        return

    try:
        workflow = json.loads(json.dumps(WORKFLOWS[workflow_name]))

        # --- Upload Phase ---
        yield None, "Uploading files..."
        uploaded_filenames = {}
        for node_id, params in files_map.items():
            for param_name, file_path in params.items():
                if file_path is None:
                    yield None, f"Error: Missing file for {param_name}"
                    return
                try:
                    with open(file_path, 'rb') as f:
                        files = {'image': f} 
                        response = requests.post(f"{base_url}/upload/image", files=files, timeout=60, headers=headers)
                        
                        if response.status_code == 200:
                            filename = response.json().get('name')
                            if node_id not in uploaded_filenames: uploaded_filenames[node_id] = {}
                            uploaded_filenames[node_id][param_name] = filename
                            print(f"[DEBUG] Uploaded {filename} for Node {node_id}")
                        else:
                            yield None, f"Upload failed: {response.text}"
                            return
                except Exception as e:
                    yield None, f"Upload error: {e}"
                    return

        # --- Configuration Phase ---
        yield None, "Configuring workflow..."
        
        def update_node(nid, key, val):
            if nid not in workflow: return
            node = workflow[nid]
            ctype = node.get("class_type", "")

            # 1. Explicit Handlers (Loaders)
            if ctype in ["LoadVideo", "LoadImage", "VHS_LoadVideo"]:
                if "widgets_values" in node and isinstance(node["widgets_values"], list):
                     if len(node["widgets_values"]) > 0:
                        node["widgets_values"][0] = val
                        return
                if "inputs" in node:
                    if key in node["inputs"]: node["inputs"][key] = val
                    elif "video" in node["inputs"]: node["inputs"]["video"] = val
                    elif "image" in node["inputs"]: node["inputs"]["image"] = val
                    elif "file" in node["inputs"]: node["inputs"]["file"] = val

            # 2. Standard Inputs (API Format)
            if "inputs" in node and isinstance(node["inputs"], dict):
                if key in node["inputs"]:
                    node["inputs"][key] = val

            # 3. Standard Widgets (Graph Format Fallback)
            if "widgets_values" in node:
                wv = node["widgets_values"]
                if isinstance(wv, list):
                    if isinstance(key, int):
                        if key < len(wv): wv[key] = val
                    elif key == "text":
                         tgt = 2 if ctype == "WanVideoTextEncodeCached" else 0
                         if tgt < len(wv): wv[tgt] = val
                elif isinstance(wv, dict):
                    if key in wv: wv[key] = val
                    elif key == "video": wv["video"] = val

        # Apply Inputs
        for node_id, params in inputs_map.items():
            for key, value in params.items():
                update_node(node_id, key, value)
        
        # Apply Files
        for node_id, params in uploaded_filenames.items():
            for key, filename in params.items():
                update_node(node_id, key, filename)

        # Randomize Seed
        for node_id, node in workflow.items():
            if "inputs" in node and isinstance(node["inputs"], dict):
                for k in ["seed", "noise_seed"]:
                    if k in node["inputs"]:
                        node["inputs"][k] = random.randint(1, 1000000000000)

        # --- Submission Phase ---
        yield None, "Sending job..."
        p = {"prompt": workflow, "client_id": client_id}
        try:
            response = requests.post(f"{base_url}/prompt", json=p, timeout=10, headers=headers)
            if response.status_code != 200:
                yield None, f"Error submitting: {response.text}"
                return
            prompt_id = response.json().get('prompt_id')
        except Exception as e:
            yield None, f"Connection error: {e}"
            return

        # --- Polling Phase ---
        print(f"Job {prompt_id} submitted to {server_address}. Waiting...")
        start_time = time.time()
        
        while True:
            time.sleep(3)
            elapsed = int(time.time() - start_time)
            yield None, f"Processing... ({elapsed}s)" 
            
            try:
                history_url = f"{base_url}/history/{prompt_id}"
                res = requests.get(history_url, timeout=10, headers=headers)
                
                if res.status_code == 200:
                    history_data = res.json()
                    if prompt_id in history_data:
                        yield None, "Job finished! Downloading..."
                        job_data = history_data[prompt_id]
                        outputs = job_data.get('outputs', {})
                        
                        # --- IMPROVED OUTPUT HUNTING STRATEGY ---
                        target_file = None
                        best_candidate = None

                        # Helper to check for video extensions
                        def is_video(fname):
                            return fname.lower().endswith(('.mp4', '.mov', '.webm', '.mkv', '.gif'))

                        # 1. Scan ALL outputs to find a Video file (Priority 1)
                        for nid, content in outputs.items():
                            for k in ['videos', 'gifs', 'files', 'images']:
                                if k in content:
                                    for item in content[k]:
                                        filename = item.get('filename', '')
                                        if is_video(filename):
                                            target_file = item # Found a video!
                                            break
                                        elif not best_candidate:
                                            best_candidate = item # Keep image as backup
                                if target_file: break
                            if target_file: break
                        
                        # 2. Use backup (image) if no video found
                        if not target_file:
                            target_file = best_candidate

                        if target_file:
                            filename = target_file['filename']
                            subfolder = target_file['subfolder']
                            file_type = target_file['type']
                            
                            view_url = f"{base_url}/view?filename={filename}&subfolder={subfolder}&type={file_type}"
                            content_res = requests.get(view_url, timeout=60, headers=headers)
                            
                            temp_dir = tempfile.gettempdir()
                            output_filepath = os.path.join(temp_dir, f"comfy_{client_id}_{filename}")
                            with open(output_filepath, 'wb') as f:
                                f.write(content_res.content)
                            
                            yield output_filepath, "Success: Video Generated."
                            return
                        else:
                            yield None, "Finished, but no output found."
                            return
            except Exception:
                continue

    except Exception as e:
        yield None, f"Error: {e}"


# --- 2. Load Workflows ---
def load_workflows(directory="workflows"):
    global WORKFLOWS
    if not os.path.exists(directory):
        print(f"Warning: '{directory}' not found.")
        return
    for filename in os.listdir(directory):
        if filename.endswith(".json"):
            filepath = os.path.join(directory, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    WORKFLOWS[os.path.splitext(filename)[0]] = json.load(f)
                    print(f"Loaded: {os.path.splitext(filename)[0]}")
            except Exception as e:
                print(f"Error loading '{filename}': {e}")

# --- 3. Connector Functions ---

def run_text_to_video(prompt):
    yield from run_comfyui_workflow(SERVER_GEN_EDIT, "t2v", {"89": {"text": prompt}}, {}, "80")

def run_long_form_video(init_image, prompt):
    if init_image is None: yield None, "Error: Upload image."; return
    yield from run_comfyui_workflow(SERVER_GEN_EDIT, "long_t2v", {"6": {"text": prompt}}, {"119": {"image": init_image}}, "79")

# --- UPDATED IMAGE TO VIDEO (Node 245 Fix + JSON IDs) ---
def run_image_to_video(init_image, prompt, strength):
    if init_image is None: yield None, "Error: Upload image."; return
    
    try:
        resized_path = init_image + "_resized.png"
        with Image.open(init_image) as img:
            max_dim = 832
            ratio = min(max_dim / img.width, max_dim / img.height)
            new_w = int(img.width * ratio)
            new_h = int(img.height * ratio)
            new_w = new_w - (new_w % 16)
            new_h = new_h - (new_h % 16)
            img_resized = img.resize((new_w, new_h), Image.LANCZOS)
            img_resized.save(resized_path)
            print(f"[I2V] Resized {img.size} -> {img_resized.size}")
            init_image = resized_path
    except Exception as e:
        print(f"[I2V] Resize failed: {e}")

    # Prompt: 6, Image: 88, Output: 63
    yield from run_comfyui_workflow(SERVER_CHARACTER_V2V, "I2V_hq_lowvram(USING)", 
        {"6": {"text": prompt}, "245": {"crop": "disabled"}}, 
        {"88": {"image": init_image}}, 
        "63" 
    )

def run_lipsync(init_image, audio_file, prompt):
    if init_image is None or audio_file is None: 
        yield None, "Error: Upload files."
        return
    try:
        with Image.open(init_image) as img:
            w, h = img.size
            aspect = w / h
            target_w, target_h = 512, 512
            if aspect < 0.8: target_w, target_h = 480, 832
            elif aspect > 1.2: target_w, target_h = 832, 480
            else: target_w, target_h = 512, 512
            print(f"Smart Resize: {w}x{h} -> {target_w}x{target_h}")
    except Exception:
        target_w, target_h = 512, 512

    inputs_map = {
        "17": {"text": prompt},
        "14": {"width": target_w, "height": target_h} 
    }
    yield from run_comfyui_workflow(SERVER_LIPSYNC, "lipsync", inputs_map, {"12": {"image": init_image}, "19": {"audio": audio_file}}, "23")

def run_img_to_img_video(first_image, last_image, prompt):
    yield from run_comfyui_workflow(SERVER_CHARACTER_V2V, "FIRST2LAST_frame", 
        {"6": {"text": prompt}}, 
        {"68": {"image": first_image}, "62": {"image": last_image}},
        "61"
    )

def run_replace_character(video_in, char_image, prompt):
    yield from run_comfyui_workflow(SERVER_CHARACTER_V2V, "replace_char", {"227": {"text": prompt}}, {"417": {"video": video_in}, "311": {"image": char_image}}, "462")

def run_move_character(video_in, char_image, prompt):
    yield from run_comfyui_workflow(SERVER_CHARACTER_V2V, "move_char", {"227": {"text": prompt}}, {"417": {"video": video_in}, "311": {"image": char_image}}, "467")

def run_control_character(video_in, control_net_image, prompt):
    yield from run_comfyui_workflow(SERVER_CHARACTER_V2V, "control_char", 
        {"179": {"text": prompt}}, 
        {"174": {"video": video_in}, "178": {"image": control_net_image}}, 
        "170"
    )

def run_inpainting(video_in, ref_image, prompt):
    yield from run_comfyui_workflow(SERVER_GEN_EDIT, "inpaint", {"129": {"text": prompt}}, {"109": {"video": video_in}, "146": {"image": ref_image}}, "135")

def run_outpainting(video_in, direction, pixels, prompt):
    pad_values = {"left": 0, "top": 0, "right": 0, "bottom": 0}
    if direction == "Left": pad_values["left"] = pixels
    elif direction == "Right": pad_values["right"] = pixels
    elif direction == "Up": pad_values["top"] = pixels
    elif direction == "Down": pad_values["bottom"] = pixels
    
    yield from run_comfyui_workflow(SERVER_CHARACTER_V2V, "outpaint", 
        {"110": pad_values, "6": {"text": prompt}}, 
        {"71": {"video": video_in}}, 
        "69"
    )

def run_remove_bg(video_in):
    if video_in is None: yield None, "Error: Upload video."; return
    yield from run_comfyui_workflow(SERVER_GEN_EDIT, "remove_bg", {}, {"1" : {"video": video_in}}, "8")


# --- 4. Gradio UI Layout ---
load_workflows(directory="workflows")

# --- CSS FIX: Force videos to NOT crop ---
custom_css = """
<style>
video {
    object-fit: contain !important;
    max-height: 80vh;
}
</style>
"""

with gr.Blocks() as demo:
    # Inject CSS here
    gr.HTML(custom_css)
    
    gr.Markdown("# Video Generation Studio (ComfyUI)")
    
    with gr.Tabs():
        with gr.TabItem("Text to Video"):
            with gr.Tabs():
                with gr.TabItem("Text to Video"):
                    with gr.Row():
                        with gr.Column(scale=2):
                            t2v_prompt = gr.Textbox(label="Prompt", lines=3, placeholder="A dog flying a kite")
                            t2v_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            t2v_output_video = gr.Video(label="Output Video", interactive=False)
                            t2v_status = gr.Textbox(label="Status", interactive=False)
                    t2v_generate_btn.click(run_text_to_video, inputs=[t2v_prompt], outputs=[t2v_output_video, t2v_status])

                with gr.TabItem("Long Form Video (Video Loop)"):
                    with gr.Row():
                        with gr.Column(scale=2):
                            long_t2v_image = gr.Image(label="Initial Image", type="filepath")
                            long_t2v_prompt = gr.Textbox(label="Prompt", lines=3)
                            long_t2v_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            long_t2v_output_video = gr.Video(label="Output Video", interactive=False)
                            long_t2v_status = gr.Textbox(label="Status", interactive=False)
                    long_t2v_generate_btn.click(run_long_form_video, inputs=[long_t2v_image, long_t2v_prompt], outputs=[long_t2v_output_video, long_t2v_status])

        with gr.TabItem("Image to Video"):
            with gr.Tabs():
                with gr.TabItem("Image to Video"):
                    with gr.Row():
                        with gr.Column(scale=2):
                            i2v_image = gr.Image(label="Initial Image", type="filepath")
                            i2v_prompt = gr.Textbox(label="Prompt", lines=2)
                            i2v_strength = gr.Slider(label="Motion Strength", minimum=0.1, maximum=1.0, value=0.5)
                            i2v_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            i2v_output_video = gr.Video(label="Output Video", interactive=False)
                            i2v_status = gr.Textbox(label="Status", interactive=False)
                    i2v_generate_btn.click(run_image_to_video, inputs=[i2v_image, i2v_prompt, i2v_strength], outputs=[i2v_output_video, i2v_status])

                with gr.TabItem("Lipsync"):
                    with gr.Row():
                        with gr.Column(scale=2):
                            lipsync_image = gr.Image(label="Face Image", type="filepath")
                            lipsync_audio = gr.Audio(label="Audio File", type="filepath")
                            lipsync_prompt = gr.Textbox(label="Prompt", lines=2)
                            lipsync_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            lipsync_output_video = gr.Video(label="Output Video", interactive=False)
                            lipsync_status = gr.Textbox(label="Status", interactive=False)
                    lipsync_generate_btn.click(run_lipsync, inputs=[lipsync_image, lipsync_audio, lipsync_prompt], outputs=[lipsync_output_video, lipsync_status])
                
                with gr.TabItem("First-to-Last Image"):
                    with gr.Row():
                        with gr.Column(scale=2):
                            i2i2v_first_image = gr.Image(label="First Image", type="filepath")
                            i2i2v_last_image = gr.Image(label="Last Image", type="filepath")
                            i2i2v_prompt = gr.Textbox(label="Prompt", lines=2)
                            i2i2v_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            i2i2v_output_video = gr.Video(label="Output Video", interactive=False)
                            i2i2v_status = gr.Textbox(label="Status", interactive=False)
                    i2i2v_generate_btn.click(run_img_to_img_video, inputs=[i2i2v_first_image, i2i2v_last_image, i2i2v_prompt], outputs=[i2i2v_output_video, i2i2v_status])

        with gr.TabItem("Video to Video"):
            with gr.Tabs():
                with gr.TabItem("Replace Character"):
                    with gr.Row():
                        with gr.Column(scale=2):
                            rc_video = gr.Video(label="Input Video", format=None)
                            rc_char_image = gr.Image(label="New Character Image", type="filepath")
                            rc_prompt = gr.Textbox(label="Prompt", lines=2)
                            rc_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            rc_output_video = gr.Video(label="Output Video", interactive=False)
                            rc_status = gr.Textbox(label="Status", interactive=False)
                    rc_generate_btn.click(run_replace_character, inputs=[rc_video, rc_char_image, rc_prompt], outputs=[rc_output_video, rc_status])

                with gr.TabItem("Move Character"):
                    with gr.Row():
                        with gr.Column(scale=2):
                            mc_video = gr.Video(label="Input Video", format=None)
                            mc_char_image = gr.Image(label="Input Character Image", type="filepath")
                            mc_prompt = gr.Textbox(label="Prompt", lines=2)
                            mc_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            mc_output_video = gr.Video(label="Output Video", interactive=False)
                            mc_status = gr.Textbox(label="Status", interactive=False)
                    mc_generate_btn.click(run_move_character, inputs=[mc_video, mc_char_image, mc_prompt], outputs=[mc_output_video, mc_status])

                # --- CONTROL CHARACTER HIDDEN ---
                with gr.TabItem("Control Character", visible=False):
                    with gr.Row():
                        with gr.Column(scale=2):
                            cc_video = gr.Video(label="Input Video", format=None)
                            cc_control_image = gr.Image(label="Control Image", type="filepath")
                            cc_prompt = gr.Textbox(label="Prompt", lines=2)
                            cc_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            cc_output_video = gr.Video(label="Output Video", interactive=False)
                            cc_status = gr.Textbox(label="Status", interactive=False)
                    cc_generate_btn.click(run_control_character, inputs=[cc_video, cc_control_image, cc_prompt], outputs=[cc_output_video, cc_status])

        with gr.TabItem("Video Inpainting"):
            with gr.Tabs():
                with gr.TabItem("Inpainting"):
                    with gr.Row():
                        with gr.Column(scale=2):
                            inp_video = gr.Video(label="Input Video", format=None)
                            inp_ref_image = gr.Image(label="Inpainted Reference Image", type="filepath")
                            inp_prompt = gr.Textbox(label="Prompt", lines=2)
                            inp_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            inp_output_video = gr.Video(label="Output Video", interactive=False)
                            inp_status = gr.Textbox(label="Status", interactive=False)
                    inp_generate_btn.click(run_inpainting, inputs=[inp_video, inp_ref_image, inp_prompt], outputs=[inp_output_video, inp_status])

                with gr.TabItem("Outpainting"):
                    with gr.Row():
                        with gr.Column(scale=2):
                            outp_video = gr.Video(label="Input Video", format=None)
                            outp_direction = gr.Radio(label="Direction", choices=["Left", "Right", "Up", "Down"], value="Right")
                            outp_pixels = gr.Slider(label="Pixels", minimum=64, maximum=512, step=16, value=128)
                            outp_prompt = gr.Textbox(label="Prompt", lines=2, placeholder="Describe the extension")
                            outp_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            outp_output_video = gr.Video(label="Output Video", interactive=False)
                            outp_status = gr.Textbox(label="Status", interactive=False)
                    outp_generate_btn.click(run_outpainting, inputs=[outp_video, outp_direction, outp_pixels, outp_prompt], outputs=[outp_output_video, outp_status])

        with gr.TabItem("Video Utilities"):
            with gr.Tabs():
                with gr.TabItem("Remove Background"):
                    with gr.Row():
                        with gr.Column(scale=2):
                            rbg_video = gr.Video(label="Input Video", format=None)
                            rbg_generate_btn = gr.Button("Generate", variant="primary")
                        with gr.Column(scale=1):
                            rbg_output_video = gr.Video(label="Output Video", interactive=False)
                            rbg_status = gr.Textbox(label="Status", interactive=False)
                    rbg_generate_btn.click(run_remove_bg, inputs=[rbg_video], outputs=[rbg_output_video, rbg_status])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=5, max_size=20)
    demo.launch(server_name="0.0.0.0", server_port=8225, show_error=True, root_path="/videos")