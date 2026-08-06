import boto3
import uuid
import logging
import sys
import info
import utils

from langchain_aws import ChatBedrock
from botocore.config import Config

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("chat")

config = utils.load_config()
bedrock_region = config['region']
accountId = config['accountId']
projectName = config['projectName']

model_name = "Claude 4.6 Sonnet"
model_type = "claude"
models = info.get_model_info(model_name)
model_id = models[0]["model_id"]

# runtime_session_id = str(uuid.uuid4())
runtime_session_id = "agentcore"
logger.info(f"runtime_session_id: {runtime_session_id}")
user_id = None 

def initiate():
    global runtime_session_id
    runtime_session_id=str(uuid.uuid4())
    logger.info(f"runtime_session_id: {runtime_session_id}")

debug_mode = 'Disable'

def update(modelName):
    global model_name, models, model_type, model_id

    if modelName != model_name:
        model_name = modelName
        logger.info(f"modelName: {modelName}")

        models = info.get_model_info(model_name)
        if not models:
            logger.error(f"Unknown model: {modelName}")
            return
        model_type = models[0]["model_type"]
        model_id = models[0]["model_id"]
        logger.info(f"model_id: {model_id}")
        logger.info(f"model_type: {model_type}")


def harness_model_config() -> dict:
    """Build InvokeHarness ``model`` override from the selected chat profile."""
    profile = models[0] if models else {}
    mid = profile.get("model_id") or model_id
    bedrock_cfg: dict = {"modelId": mid}
    api_format = profile.get("mantle_api") or profile.get("apiFormat")
    if api_format:
        bedrock_cfg["apiFormat"] = api_format
    return {"bedrockModelConfig": bedrock_cfg}

def get_chat(extended_thinking=None):
    # Set default value if not provided or invalid
    if extended_thinking is None or extended_thinking not in ['Enable', 'Disable']:
        extended_thinking = 'Disable'

    logger.info(f"model_name: {model_name}")
    profile = models[0]
    bedrock_region =  profile['bedrock_region']
    modelId = profile['model_id']
    model_type = profile['model_type']
    maxOutputTokens = 4096 # 4k
    logger.info(f"LLM: bedrock_region: {bedrock_region}, modelId: {modelId}, model_type: {model_type}")

    STOP_SEQUENCE = "\n\nHuman:" 
                          
    # bedrock   
    boto3_bedrock = boto3.client(
        service_name='bedrock-runtime',
        region_name=bedrock_region,
        config=Config(
            retries = {
                'max_attempts': 30
            }
        )
    )
    
    if extended_thinking=='Enable':
        maxReasoningOutputTokens=64000
        logger.info(f"extended_thinking: {extended_thinking}")
        thinking_budget = min(maxOutputTokens, maxReasoningOutputTokens-1000)

        parameters = {
            "max_tokens":maxReasoningOutputTokens,
            "temperature":1,            
            "thinking": {
                "type": "enabled",
                "budget_tokens": thinking_budget
            },
            "stop_sequences": [STOP_SEQUENCE]
        }
    else:
        parameters = {
            "max_tokens":maxOutputTokens,     
            "temperature":0.1,
            "top_k":250,
            "stop_sequences": [STOP_SEQUENCE]
        }

    chat = ChatBedrock(   # new chat model
        model_id=modelId,
        client=boto3_bedrock, 
        model_kwargs=parameters,
        region_name=bedrock_region
    )    
    return chat

