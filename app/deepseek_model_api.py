import traceback
import math
import boto3
import time
import argparse
import torch
import torch.nn as nn
import torch_neuronx
import neuronx_distributed
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional, Union
from huggingface_hub import login
from starlette.responses import StreamingResponse
import base64

cw_namespace='hw-agnostic-infer'
cloudwatch = boto3.client('cloudwatch', region_name='us-west-2')

app_name=os.environ['APP']
nodepool=os.environ['NODEPOOL']
model_id = os.environ['MODEL_ID']
compiled_model_id=os.environ['COMPILED_MODEL_ID']
device = os.environ["DEVICE"]
pod_name = os.environ['POD_NAME']
hf_token = os.environ['HUGGINGFACE_TOKEN'].strip()
default_max_new_tokens=int(os.environ['MAX_NEW_TOKENS'])

from transformers import AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained(model_id)
tokenizer.pad_token_id = tokenizer.eos_token_id if tokenizer.pad_token_id is None else tokenizer.pad_token_id


if device=='xla':
  from optimum.neuron import NeuronModelForCausalLM
elif device=='cuda':
  from transformers import AutoModelForCausalLM,BitsAndBytesConfig
  quantization_config = BitsAndBytesConfig(load_in_4bit=True,bnb_4bit_use_double_quant=True,bnb_4bit_compute_dtype=torch.float16)
elif device == 'cpu':
  from transformers import AutoModelForCausalLM

def gentext(prompt,max_new_tokens):
  start_time = time.time()
  if device=='xla':
    inputs = tokenizer(prompt, return_tensors="pt")
  elif device=='cuda':
    inputs = tokenizer(prompt, return_tensors="pt").to('cuda')
  elif device=='cpu':
    inputs = tokenizer(prompt, return_tensors="pt").to('cpu')
  outputs = model.generate(**inputs,max_new_tokens=max_new_tokens,do_sample=True,use_cache=True,temperature=0.7,top_k=50,top_p=0.9)
  outputs = outputs[0, inputs.input_ids.size(-1):]
  response = tokenizer.decode(outputs, skip_special_tokens=True)
  total_time =  time.time()-start_time
  return str(response), float(total_time)

def cw_pub_metric(metric_name,metric_value,metric_unit):
  response = cloudwatch.put_metric_data(
    Namespace=cw_namespace,
    MetricData=[
      {
        'MetricName':metric_name,
        'Value':metric_value,
        'Unit':metric_unit,
       },
    ]
  )
  print(f"in pub_deployment_counter - response:{response}")
  return response

# Login to Hugging Face
login(hf_token, add_to_git_credential=True)

# TBD change to text from image
def benchmark(n_runs, test_name,model,prompt,max_new_tokens):
    if device=='xla':
      inputs = tokenizer(prompt, return_tensors="pt")
    elif device=='cuda':
      inputs = tokenizer(prompt, return_tensors="pt").to('cuda')
    elif device=='cpu':
      inputs = tokenizer(prompt, return_tensors="pt").to('cpu')

    warmup_run = model.generate(**inputs,max_new_tokens=max_new_tokens,do_sample=True,use_cache=True,temperature=0.7,top_k=50,top_p=0.9)
    latency_collector = LatencyCollector()

    for _ in range(n_runs):
        latency_collector.pre_hook()
        res = model.generate(**inputs,max_new_tokens=max_new_tokens,do_sample=True,use_cache=True,temperature=0.7,top_k=50,top_p=0.9)
        latency_collector.hook()

    p0_latency_ms = latency_collector.percentile(0) * 1000
    p50_latency_ms = latency_collector.percentile(50) * 1000
    p90_latency_ms = latency_collector.percentile(90) * 1000
    p95_latency_ms = latency_collector.percentile(95) * 1000
    p99_latency_ms = latency_collector.percentile(99) * 1000
    p100_latency_ms = latency_collector.percentile(100) * 1000

    report_dict = dict()
    report_dict["Latency P0"] = f'{p0_latency_ms:.1f}'
    report_dict["Latency P50"]=f'{p50_latency_ms:.1f}'
    report_dict["Latency P90"]=f'{p90_latency_ms:.1f}'
    report_dict["Latency P95"]=f'{p95_latency_ms:.1f}'
    report_dict["Latency P99"]=f'{p99_latency_ms:.1f}'
    report_dict["Latency P100"]=f'{p100_latency_ms:.1f}'

    report = f'RESULT FOR {test_name}:'
    for key, value in report_dict.items():
        report += f' {key}={value}'
    print(report)
    return report

class LatencyCollector:
    def __init__(self):
        self.start = None
        self.latency_list = []

    def pre_hook(self, *args):
        self.start = time.time()

    def hook(self, *args):
        self.latency_list.append(time.time() - self.start)

    def percentile(self, percent):
        latency_list = self.latency_list
        pos_float = len(latency_list) * percent / 100
        max_pos = len(latency_list) - 1
        pos_floor = min(math.floor(pos_float), max_pos)
        pos_ceil = min(math.ceil(pos_float), max_pos)
        latency_list = sorted(latency_list)
        return latency_list[pos_ceil] if pos_float - pos_floor > 0.5 else latency_list[pos_floor]

class GenerateRequest(BaseModel):
    max_new_tokens: int
    prompt: str

class GenerateBenchmarkRequest(BaseModel):
    n_runs: int
    max_new_tokens: int
    prompt: str

class GenerateResponse(BaseModel):
    text: str = Field(..., description="Base64-encoded text")
    execution_time: float

class GenerateBenchmarkResponse(BaseModel):
    report: str = Field(..., description="Benchmark report")

def load_model():
  if device=='xla':
    model = NeuronModelForCausalLM.from_pretrained(compiled_model_id)
  elif device=='cuda':
    model = AutoModelForCausalLM.from_pretrained(model_id,use_cache=True,device_map='auto',torch_dtype=torch.float16,quantization_config=quantization_config,)
  elif device=='cpu':
    model=AutoModelForCausalLM.from_pretrained(model_id)  
  return model

model = load_model()
prompt= "What model are you?"
benchmark(10,"warmup",model,prompt,default_max_new_tokens)
app = FastAPI()

@app.post("/benchmark",response_model=GenerateBenchmarkResponse) 
def generate_benchmark_report(request: GenerateBenchmarkRequest):
  try:
      with torch.no_grad():
        test_name=f'benchmark:{model_id} on {device} with {request.max_new_tokens} output tokens'
        response_report=benchmark(request.n_runs,request.max_new_tokens,request.prompt,test_name)
        report_base64 = base64.b64encode(response_report.encode()).decode()
      return GenerateBenchmarkResponse(report=report_base64)
  except Exception as e:
      traceback.print_exc()
      raise HTTPException(status_code=500, detail=f"{e}")

@app.post("/generate", response_model=GenerateResponse)
def generate_text_post(request: GenerateRequest):
  try:
      with torch.no_grad():
        response_text,total_time=gentext(request.prompt,request.max_new_tokens)
      counter_metric=app_name+'-counter'
      cw_pub_metric(counter_metric,1,'Count')
      counter_metric=nodepool
      cw_pub_metric(counter_metric,1,'Count')
      latency_metric=app_name+'-latency'
      cw_pub_metric(latency_metric,total_time,'Seconds')
      text_base64 = base64.b64encode(response_text.encode()).decode()
      return GenerateResponse(text=text_base64, execution_time=total_time)
  except Exception as e:
      traceback.print_exc()
      raise HTTPException(status_code=500, detail=f"text serialization failed: {e}")

# Health and readiness endpoints
@app.get("/health")
def healthy():
    return {"message": f"{pod_name} is healthy"}

@app.get("/readiness")
def ready():
    return {"message": f"{pod_name} is ready"}
