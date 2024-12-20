import os
import math
import time
import random
import gradio as gr
from matplotlib import image as mpimg
from fastapi import FastAPI
import torch
from huggingface_hub import login
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

pod_name=os.environ['POD_NAME']
model_id=os.environ['MODEL_ID']
compiled_model_id=os.environ['COMPILED_MODEL_ID']
device=os.environ["DEVICE"]
hf_token=os.environ['HUGGINGFACE_TOKEN'].strip()
max_new_tokens=int(os.environ['MAX_NEW_TOKENS'])
min_new_tokens = max_new_tokens-1

login(hf_token,add_to_git_credential=True)

if device=='xla':
  from optimum.neuron import NeuronModelForCausalLM
elif device=='cuda':
  # from transformers import AutoModelForCausalLM,BitsAndBytesConfig
  from transformers import AutoModelForCausalLM
  # quantization_config = BitsAndBytesConfig(load_in_4bit=True,bnb_4bit_use_double_quant=True,bnb_4bit_compute_dtype=torch.float16)
elif device == 'cpu':
  from transformers import AutoModelForCausalLM

from transformers import AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained(model_id)
tokenizer.pad_token_id = tokenizer.eos_token_id if tokenizer.pad_token_id is None else tokenizer.pad_token_id

def gentext(prompt):
  start_time = time.time()
  if device=='xla':
    inputs = tokenizer(prompt, return_tensors="pt")
  elif device=='cuda':
    inputs = tokenizer(prompt, return_tensors="pt").to('cuda')
  elif device=='cpu':
    inputs = tokenizer(prompt, return_tensors="pt").to('cpu')
  outputs = model.generate(**inputs,min_new_tokens=min_new_tokens,max_new_tokens=max_new_tokens,do_sample=True,use_cache=True,top_k=50,top_p=0.9)
  outputs = outputs[0, inputs.input_ids.size(-1):]
  response = tokenizer.decode(outputs, skip_special_tokens=True)
  total_time =  time.time()-start_time
  return str(response), str(total_time)

def classify_sentiment(prompt):
  response,total_time=gentext(f"Classify the sentiment of the following text as positive, negative, or neutral:\n\n{prompt}\n\nSentiment:")
  sentiment = response.split("Sentiment:")[-1].strip()
  return sentiment,total_time

if device=='xla':
  model = NeuronModelForCausalLM.from_pretrained(compiled_model_id)
elif device=='cuda': 
  # model = AutoModelForCausalLM.from_pretrained(model_id,use_cache=True,device_map='auto',torch_dtype=torch.float16,quantization_config=quantization_config,)
  model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=torch.float16).to('cuda')
elif device=='cpu':
  model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=torch.float16).to('cpu')

gentext("write a poem")


app = FastAPI()

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # or ["*"] to allow all
    allow_credentials=True,
    allow_methods=["*"],  # allow all HTTP methods
    allow_headers=["*"],  # allow all headers
)

io = gr.Interface(fn=gentext,inputs=["text"],
    outputs = ["text","text"],
    title = model_id + ' in AWS EC2 ' + device + ' instance; pod name ' + pod_name)

@app.get("/")
def read_main():
  return {"message": "This is" + model_id + " pod " + pod_name + " in AWS EC2 " + device + " instance; try /load/{n_runs}/infer/{n_inf}; /gentext http post with user prompt "}

class Item(BaseModel):
  prompt: str
  response: str=None
  latency: float=0.0

@app.post("/gentext")
def generate_text_post(item: Item):
  item.response,item.latency=gentext(item.prompt)
  return {"prompt":item.prompt,"response":item.response,"latency":item.latency}

@app.post("/sentiment")
def classify_text_post(item: Item):
  item.response,item.latency=classify_sentiment(item.prompt)
  return {"prompt":item.prompt,"response":item.response,"latency":item.latency}

@app.get("/health")
def healthy():
  return {"message": pod_name + "is healthy"}

@app.get("/readiness")
def ready():
  return {"message": pod_name + "is ready"}

app = gr.mount_gradio_app(app, io, path="/serve")
