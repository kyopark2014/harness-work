import boto3
from botocore.config import Config
from botocore.exceptions import EventStreamError
import json
import os
import logging
import re
import sys
import uuid

# Import utils from application package
try:
    from application import utils
except ImportError:
    import utils

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("agentcore_client")

config = utils.load_config()

bedrock_region = config['region']
accountId = config['accountId']
projectName = config['projectName']

# Same rule as installer.harness_name_for_api: harness name only ('-' → '_').
_HARNESS_NAME_API_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")


def harness_name_for_api(project_name: str) -> str | None:
    """Map projectName to CreateHarness harnessName ('-' → '_')."""
    normalized = (project_name or "").replace("-", "_")
    if not _HARNESS_NAME_API_RE.fullmatch(normalized):
        logger.error(
            f"harness_name_for_api: invalid after '-'→'_': {normalized!r}, "
            f"projectName: {project_name!r}"
        )
        return None
    return normalized


def _normalize_harness_api_name(value: str) -> str | None:
    """Normalize harnessName lookup key ('-' → '_')."""
    n = (value or "").strip().replace("-", "_")
    if not _HARNESS_NAME_API_RE.fullmatch(n):
        logger.error(
            f"_normalize_harness_api_name: invalid: {n!r} "
            f"(must match [a-zA-Z][a-zA-Z0-9_]{{0,39}})"
        )
        return None
    return n


def _list_all_harness_summaries(control) -> list:
    items = []
    token = None
    while True:
        kw: dict = {"maxResults": 50}
        if token:
            kw["nextToken"] = token
        resp = control.list_harnesses(**kw)
        items.extend(resp.get("harnesses") or [])
        token = resp.get("nextToken")
        if not token:
            break
    return items


def _arn_from_harness_summary(control, h: dict) -> str | None:
    arn = h.get("arn")
    if arn:
        return arn
    hid = h.get("harnessId")
    if not hid:
        return None
    full = control.get_harness(harnessId=hid).get("harness") or {}
    return full.get("arn")


def resolve_harness_arn_from_config(cfg: dict, region: str) -> str | None:
    """
    Resolve Harness ARN via bedrock-agentcore-control ListHarnesses.

    Lookup key: optional cfg["harnessName"], else derived from cfg["projectName"].
    If no name match but exactly one harness exists in the region, use it (log warning).
    """
    override = cfg.get("harnessName")
    if override:
        api_name = _normalize_harness_api_name(str(override))
    else:
        api_name = harness_name_for_api(cfg.get("projectName") or "")
    if not api_name:
        return None

    control = boto3.client("bedrock-agentcore-control", region_name=region)
    summaries = _list_all_harness_summaries(control)
    available = sorted(
        {n for n in (h.get("harnessName") for h in summaries) if n}
    )

    for h in summaries:
        if h.get("harnessName") != api_name:
            continue
        arn = _arn_from_harness_summary(control, h)
        if arn:
            logger.info(
                f"HARNESS_ARN: resolved: harnessName: {api_name!r}, arn: {arn}"
            )
            return arn

    if len(summaries) == 1:
        only = summaries[0]
        only_name = only.get("harnessName")
        arn = _arn_from_harness_summary(control, only)
        if arn:
            logger.warning(
                f"harness lookup: no match for harnessName: {api_name!r}, "
                f"region: {region}, using only harness: {only_name!r}, "
                f"set harnessName or HARNESS_ARN if you add more harnesses"
            )
            return arn

    logger.error(
        f"harness lookup: no harness named: {api_name!r}, region: {region}, "
        f"available harnessName: {available}, "
        f"hint: align projectName, set harnessName, or set HARNESS_ARN in application/config.json"
    )
    return None


def add_notification(notification_queue, message):
    if notification_queue is not None:
        notification_queue.notify(message)

def update_streaming_result(notification_queue, message):
    if notification_queue is not None:
        notification_queue.stream(message)

def commit_streaming_segment(notification_queue, message: str):
    if notification_queue is not None:
        notification_queue.commit_text_segment(message)

def on_tool_use_started(
    notification_queue,
    current: str,
    tool_use_id: str,
    tool_info_list: dict,
) -> str:
    """Commit pre-tool assistant text when a new tool call starts.

    Resets the streaming accumulator so later tokens are a new segment.
    Without this, mid-turn text is concatenated across tools and the UI
    collapses intermediate messages to the end of the turn.
    """
    if not tool_use_id or tool_use_id in tool_info_list:
        return current
    commit_streaming_segment(notification_queue, current)
    tool_info_list[tool_use_id] = True
    return ""

def tool_slot_update(notification_queue, slot_key: str, message: str):
    if notification_queue is not None:
        notification_queue.tool_update(slot_key, message)

def load_agentcore_config(agent_name):
    client = boto3.client('bedrock-agentcore-control', region_name=bedrock_region)
    response = client.list_agent_runtimes()
    logger.info(f"response: {response}")

    agentRuntimes = response['agentRuntimes']
    for agentRuntime in agentRuntimes:
        if agentRuntime['agentRuntimeName'] == agent_name:
            return agentRuntime['agentRuntimeArn']
    return None

runtime_session_id = str(uuid.uuid4())
logger.info(f"runtime_session_id: {runtime_session_id}")

tool_info_list = dict()
tool_result_list = dict()
tool_name_list = dict()


def _build_tool_reference(ref_item: dict) -> dict:
    """Build a display reference from a RAG doc item."""
    reference = ref_item.get("reference") or {}
    contents = ref_item.get("contents") or ""
    content_text = contents[:100] + "..." if len(contents) > 100 else contents
    result = {
        "url": reference.get("url"),
        "title": reference.get("title"),
        "content": content_text,
    }
    if reference.get("page") is not None:
        result["page"] = reference["page"]
    return result


def get_tool_info(tool_name, tool_content):
    tool_references = []    
    urls = []
    content = ""

    # tavily
    if isinstance(tool_content, str) and "Title:" in tool_content and "URL:" in tool_content and "Content:" in tool_content:
        logger.info("Tavily parsing...")
        items = tool_content.split("\n\n")
        for i, item in enumerate(items):
            # logger.info(f"item[{i}]: {item}")
            if "Title:" in item and "URL:" in item and "Content:" in item:
                try:
                    title_part = item.split("Title:")[1].split("URL:")[0].strip()
                    url_part = item.split("URL:")[1].split("Content:")[0].strip()
                    content_part = item.split("Content:")[1].strip().replace("\n", "")
                    
                    logger.info(f"title_part: {title_part}")
                    logger.info(f"url_part: {url_part}")
                    logger.info(f"content_part: {content_part}")

                    content += f"{content_part}\n\n"
                    
                    tool_references.append({
                        "url": url_part,
                        "title": title_part,
                        "content": content_part[:100] + "..." if len(content_part) > 100 else content_part
                    })
                except Exception as e:
                    logger.info(f"Parsing error: {str(e)}")
                    continue                

    # OpenSearch
    elif tool_name == "SearchIndexTool": 
        if ":" in tool_content:
            extracted_json_data = tool_content.split(":", 1)[1].strip()
            try:
                json_data = json.loads(extracted_json_data)
                # logger.info(f"extracted_json_data: {extracted_json_data[:200]}")
            except json.JSONDecodeError:
                logger.info("JSON parsing error")
                json_data = {}
        else:
            json_data = {}
        
        if "hits" in json_data:
            hits = json_data["hits"]["hits"]
            if hits:
                logger.info(f"hits[0]: {hits[0]}")

            for hit in hits:
                text = hit["_source"]["text"]
                metadata = hit["_source"]["metadata"]
                
                content += f"{text}\n\n"

                filename = metadata["name"].split("/")[-1]
                # logger.info(f"filename: {filename}")
                
                content_part = text.replace("\n", "")
                tool_references.append({
                    "url": metadata["url"], 
                    "title": filename,
                    "content": content_part[:100] + "..." if len(content_part) > 100 else content_part
                })
                
        logger.info(f"content: {content}")
        
    # Knowledge Base
    elif tool_name == "QueryKnowledgeBases": 
        try:
            # Handle case where tool_content contains multiple JSON objects
            if tool_content.strip().startswith('{'):
                # Parse each JSON object individually
                json_objects = []
                current_pos = 0
                brace_count = 0
                start_pos = -1
                
                for i, char in enumerate(tool_content):
                    if char == '{':
                        if brace_count == 0:
                            start_pos = i
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0 and start_pos != -1:
                            try:
                                json_obj = json.loads(tool_content[start_pos:i+1])
                                # logger.info(f"json_obj: {json_obj}")
                                json_objects.append(json_obj)
                            except json.JSONDecodeError:
                                logger.info(f"JSON parsing error: {tool_content[start_pos:i+1][:100]}")
                            start_pos = -1
                
                json_data = json_objects
            else:
                # Try original method
                json_data = json.loads(tool_content)                
            # logger.info(f"json_data: {json_data}")

            # Build content
            if isinstance(json_data, list):
                for item in json_data:
                    if isinstance(item, dict) and "content" in item:
                        content_text = item["content"].get("text", "")
                        content += content_text + "\n\n"

                        uri = "" 
                        if "location" in item:
                            if "s3Location" in item["location"]:
                                uri = item["location"]["s3Location"]["uri"]
                                # logger.info(f"uri (list): {uri}")
                                ext = uri.split(".")[-1]

                                # # if ext is an image 
                                # url = sharing_url + "/" + s3_prefix + "/" + uri.split("/")[-1]
                                # if ext in ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "ico", "webp"]:
                                #     url = sharing_url + "/" + capture_prefix + "/" + uri.split("/")[-1]
                                # logger.info(f"url: {url}")
                                
                                tool_references.append({
                                    "url": url, 
                                    "title": uri.split("/")[-1],
                                    "content": content_text[:100] + "..." if len(content_text) > 100 else content_text
                                })          
                
        except json.JSONDecodeError as e:
            logger.info(f"JSON parsing error: {e}")
            json_data = {}
            content = tool_content  # Use original content if parsing fails

        logger.info(f"content: {content}")
        logger.info(f"tool_references: {tool_references}")

    # aws document
    elif tool_name == "search_documentation":
        try:
            # Handle case where tool_content is already a list (e.g., from toolResult)
            if isinstance(tool_content, list):
                # Extract text from list items if they have 'text' key
                json_data = []
                for item in tool_content:
                    if isinstance(item, dict) and 'text' in item:
                        try:
                            parsed_text = json.loads(item['text'])
                            if isinstance(parsed_text, dict) and 'search_results' in parsed_text:
                                json_data = parsed_text['search_results']
                            elif isinstance(parsed_text, list):
                                json_data = parsed_text
                            else:
                                json_data.append(parsed_text)
                        except (json.JSONDecodeError, TypeError):
                            logger.info(f"Failed to parse text from list item: {item}")
                    elif isinstance(item, dict):
                        json_data.append(item)
                    else:
                        json_data.append(item)
            elif isinstance(tool_content, str):
                json_data = json.loads(tool_content)
            else:
                json_data = tool_content
            
            # Ensure json_data is iterable
            if not isinstance(json_data, list):
                json_data = [json_data]
            
            for item in json_data:
                logger.info(f"item: {item}")
                
                if isinstance(item, str):
                    try:
                        item = json.loads(item)
                    except json.JSONDecodeError:
                        logger.info(f"Failed to parse item as JSON: {item}")
                        continue
                
                if isinstance(item, dict) and 'url' in item and 'title' in item:
                    url = item['url']
                    title = item['title']
                    context_text = item.get('context', '')
                    content_text = context_text[:100] + "..." if len(context_text) > 100 else context_text
                    content += context_text + "\n\n"
                    tool_references.append({
                        "url": url,
                        "title": title,
                        "content": content_text
                    })
                else:
                    logger.info(f"Invalid item format: {item}")
                    
        except json.JSONDecodeError as e:
            logger.info(f"JSON parsing error: {e}, tool_content type: {type(tool_content)}")
            pass
        except Exception as e:
            logger.error(f"Error processing search_documentation: {e}")
            pass

        logger.info(f"content: {content}")
        logger.info(f"tool_references: {tool_references}")
            
    # ArXiv
    elif tool_name == "search_papers" and "papers" in tool_content:
        try:
            json_data = json.loads(tool_content)

            papers = json_data['papers']
            for paper in papers:
                url = paper['url']
                title = paper['title']
                abstract = paper['abstract'].replace("\n", "")
                content_text = abstract[:100] + "..." if len(abstract) > 100 else abstract
                content += f"{content_text}\n\n"
                logger.info(f"url: {url}, title: {title}, content: {content_text}")

                tool_references.append({
                    "url": url,
                    "title": title,
                    "content": content_text
                })
        except json.JSONDecodeError:
            logger.info(f"JSON parsing error: {tool_content}")
            pass

        logger.info(f"content: {content}")
        logger.info(f"tool_references: {tool_references}")

    # aws-knowledge
    elif tool_name == "aws___read_documentation":
        logger.info(f"#### {tool_name} ####")
        if isinstance(tool_content, dict):
            json_data = tool_content
        elif isinstance(tool_content, list):
            json_data = tool_content
        else:
            json_data = json.loads(tool_content)
        
        logger.info(f"json_data: {json_data}")
        payload = json_data["response"]["payload"]
        if "content" in payload:
            payload_content = payload["content"]
            if "result" in payload_content:
                result = payload_content["result"]
                logger.info(f"result: {result}")
                if isinstance(result, str) and "AWS Documentation from" in result:
                    logger.info(f"Processing AWS Documentation format: {result}")
                    try:
                        # Extract URL from "AWS Documentation from https://..."
                        url_start = result.find("https://")
                        if url_start != -1:
                            # Find the colon after the URL (not inside the URL)
                            url_end = result.find(":", url_start)
                            if url_end != -1:
                                # Check if the colon is part of the URL or the separator
                                url_part = result[url_start:url_end]
                                # If the colon is immediately after the URL, use it as separator
                                if result[url_end:url_end+2] == ":\n":
                                    url = url_part
                                    content_start = url_end + 2  # Skip the colon and newline
                                else:
                                    # Try to find the actual URL end by looking for space or newline
                                    space_pos = result.find(" ", url_start)
                                    newline_pos = result.find("\n", url_start)
                                    if space_pos != -1 and newline_pos != -1:
                                        url_end = min(space_pos, newline_pos)
                                    elif space_pos != -1:
                                        url_end = space_pos
                                    elif newline_pos != -1:
                                        url_end = newline_pos
                                    else:
                                        url_end = len(result)
                                    
                                    url = result[url_start:url_end]
                                    content_start = url_end + 1
                                
                                # Remove trailing colon from URL if present
                                if url.endswith(":"):
                                    url = url[:-1]
                                
                                # Extract content after the URL
                                if content_start < len(result):
                                    content_text = result[content_start:].strip()
                                    # Truncate content for display
                                    display_content = content_text[:100] + "..." if len(content_text) > 100 else content_text
                                    display_content = display_content.replace("\n", "")
                                    
                                    tool_references.append({
                                        "url": url,
                                        "title": "AWS Documentation",
                                        "content": display_content
                                    })
                                    content += content_text + "\n\n"
                                    logger.info(f"Extracted URL: {url}")
                                    logger.info(f"Extracted content length: {len(content_text)}")
                    except Exception as e:
                        logger.error(f"Error parsing AWS Documentation format: {e}")
        logger.info(f"content: {content}")
        logger.info(f"tool_references: {tool_references}")

    else:        
        try:
            if isinstance(tool_content, dict):
                json_data = tool_content
            elif isinstance(tool_content, list):
                json_data = tool_content
            else:
                json_data = json.loads(tool_content)
            
            logger.info(f"json_data: {json_data}")
            if isinstance(json_data, dict) and "path" in json_data:  # path
                path = json_data["path"]
                if isinstance(path, list):
                    for url in path:
                        urls.append(url)
                else:
                    urls.append(path)            

            if isinstance(json_data, dict):
                for item in json_data:
                    logger.info(f"item: {item}")
                    if "reference" in item and "contents" in item:
                        tool_references.append(_build_tool_reference(item))
            else:
                logger.info(f"json_data is not a dict: {json_data}")

                for item in json_data:
                    if "reference" in item and "contents" in item:
                        tool_references.append(_build_tool_reference(item))
                
            logger.info(f"tool_references: {tool_references}")

        except json.JSONDecodeError:
            pass

    return content, urls, tool_references

def _json_preview(obj, max_len: int = 2400) -> str:
    """Compact JSON (or repr) for logs; truncate long payloads."""
    try:
        if isinstance(obj, (bytes, bytearray)):
            return f"<binary {len(obj)} bytes>"
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(obj)
    if len(s) > max_len:
        return s[:max_len] + f"... (+{len(s) - max_len} chars)"
    return s


def _image_format_from_name(file_name: str) -> str:
    lower = (file_name or "").lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "jpeg"
    if lower.endswith(".gif"):
        return "gif"
    if lower.endswith(".webp"):
        return "webp"
    return "png"


def _describe_images_with_bedrock(prompt: str, files: list[str]) -> str:
    """
    InvokeHarness content blocks are text-only, so describe attached images
    with Bedrock Converse (vision) and inject the result into the harness prompt.
    """
    if not files:
        return ""

    content_blocks: list[dict] = []
    names: list[str] = []
    for file_ref in files:
        try:
            file_name, image_bytes = utils.load_image_bytes_from_ref(file_ref)
            fmt = _image_format_from_name(file_name)
            content_blocks.append(
                {
                    "image": {
                        "format": fmt,
                        "source": {"bytes": image_bytes},
                    }
                }
            )
            names.append(file_name)
            logger.info("vision describe: loaded %s (%s bytes)", file_name, len(image_bytes))
        except Exception as exc:
            logger.warning("vision describe: failed to load %s: %s", file_ref, exc)
            content_blocks.append(
                {
                    "text": f"(이미지 로드 실패: {file_ref} — {exc})",
                }
            )

    if not any("image" in block for block in content_blocks):
        return ""

    user_text = (prompt or "").strip() or "첨부한 이미지를 자세히 설명해주세요."
    content_blocks.append(
        {
            "text": (
                "다음 첨부 이미지를 자세히 분석하세요. 구성 요소, 텍스트/레이블, "
                "화살표·연결 관계, 전체 의미를 markdown으로 정리하세요.\n"
                f"사용자 요청: {user_text}"
            )
        }
    )

    try:
        import chat as chat_mod

        model_id = getattr(chat_mod, "model_id", None) or (
            "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        # Prefer a fast vision-capable model for enrichment
        if "opus" in str(model_id).lower() or "sonnet" in str(model_id).lower():
            vision_model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        else:
            vision_model = model_id

        runtime = boto3.client("bedrock-runtime", region_name=bedrock_region)
        response = runtime.converse(
            modelId=vision_model,
            messages=[{"role": "user", "content": content_blocks}],
            inferenceConfig={"maxTokens": 4096, "temperature": 0.2},
        )
        parts = []
        for block in response.get("output", {}).get("message", {}).get("content", []):
            text = block.get("text")
            if text:
                parts.append(text)
        description = "\n".join(parts).strip()
        if not description:
            return ""
        label = ", ".join(names) if names else "첨부 이미지"
        return (
            f"[첨부 이미지 분석: {label}]\n{description}\n"
            f"[이미지 URL]\n" + "\n".join(f"- {u}" for u in files)
        )
    except Exception as exc:
        logger.warning("vision describe via Converse failed: %s", exc)
        urls = "\n".join(f"- {u}" for u in files)
        return f"[첨부 이미지 URL]\n{urls}"


def build_harness_prompt_with_files(prompt: str, files: list | None = None) -> str:
    """Merge user text with vision description of attached image URLs."""
    text = (prompt or "").strip()
    file_list = [str(f).strip() for f in (files or []) if str(f).strip()]
    if not file_list:
        return text
    if not text:
        text = "첨부한 이미지를 분석해주세요."
    description = _describe_images_with_bedrock(text, file_list)
    if description:
        return f"{text}\n\n---\n{description}"
    urls = "\n".join(f"- {u}" for u in file_list)
    return f"{text}\n\n[첨부 이미지 URL]\n{urls}"


_HARNESS_SYSTEM_PROMPT_BASE = (
    "당신의 이름은 서연이고, 질문에 친근한 방식으로 대답하도록 설계된 대화형 AI입니다.\n"
    "상황에 맞는 구체적인 세부 정보를 충분히 제공합니다.\n"
    "모르는 질문을 받으면 솔직히 모른다고 말합니다.\n"
    "한국어로 답변하세요.\n"
    "\n"
    "## Runtime environment\n"
    "- 이 환경에는 Node.js/npm이 없습니다. `node`, `npm`, `npx`를 시도하지 마세요 "
    "(command not found / exit 127).\n"
    "- 문서·슬라이드·스프레드시트 생성은 처음부터 Python을 사용하세요 "
    "(예: python-docx, python-pptx, openpyxl). "
    "docx-js / pptxgenjs 등 Node 패키지 경로는 사용하지 마세요.\n"
    "- 필요 시 `pip3 install python-docx` / `pip3 install python-pptx` 등으로 설치한 뒤 바로 생성하세요.\n"
    "\n"
    "## Shell / Python packages\n"
    "- Python 패키지 설치·실행에는 반드시 pip3를 사용하세요. pip는 이 환경에 없습니다.\n"
    "- 예: pip3 install <package>, pip3 show <package> (pip 금지)\n"
    "\n"
    "## Artifact sharing (REQUIRED)\n"
    "- ARTIFACTS_DIR에 PPT/PDF/DOCX/XLSX/PNG/CSV/HTML 등 결과 파일을 생성했다면, "
    "사용자에게 최종 답변하기 **전에** 반드시 artifact-share MCP의 ``share_artifact``를 호출하세요.\n"
    "- 로컬 경로(`/mnt/workspace/...`, ARTIFACTS_DIR)만 안내하는 것은 **금지**입니다. "
    "사용자는 그 경로에 접근할 수 없습니다.\n"
    "- ``share_artifact``가 반환한 CloudFront 공유 URL을 최종 답변에 **반드시** 포함하세요. "
    "URL 없이 '생성 완료'만 말하면 실패입니다.\n"
    "- 파일이 여러 개면 파일마다 ``share_artifact``를 각각 호출하세요.\n"
    "- 인자: filepath는 'artifacts/파일명' 또는 ARTIFACTS_DIR 절대경로; "
    "actor_id는 시스템 프롬프트 값을 그대로 사용 (필수).\n"
    "\n"
    "## Agent Workflow\n"
    "1. 사용자 입력을 받는다\n"
    "2. 요청에 맞는 skill/도구가 있으면 해당 지침에 따라 작업을 수행한다\n"
    "3. 코드 실행·파일 생성 시 반드시 ARTIFACTS_DIR(actor별 폴더) 아래에 산출물을 저장한다\n"
    "4. 결과 파일이 있으면 사용자 답변 전에 반드시 artifact-share MCP의 "
    "``share_artifact``를 호출하고, 반환된 공유 URL을 답변에 포함한다 "
    "(로컬 경로만 안내 금지; filepath는 'artifacts/파일명' 또는 ARTIFACTS_DIR 절대경로; "
    "actor_id 필수)\n"
    "5. 공유 URL을 포함한 최종 결과를 사용자에게 전달한다\n"
)


def build_harness_system_prompt(actor_id: str | None = None) -> list[dict]:
    """Build InvokeHarness systemPrompt with actor_id and ARTIFACTS_DIR paths."""
    text = _HARNESS_SYSTEM_PROMPT_BASE
    aid = (actor_id or "").strip()
    if aid:
        artifacts_dir = f"/mnt/workspace/{aid}/artifacts"
        text += (
            "\n"
            f"actor_id: {aid}\n"
            "\n"
            "## Paths (use absolute paths when writing files)\n"
            f"- SESSION_STORAGE_DIR: /mnt/workspace\n"
            f"- ARTIFACTS_DIR: {artifacts_dir}\n"
            f"모든 산출물(PDF, DOCX, PPTX, PNG, CSV, drawio 등)은 반드시 ARTIFACTS_DIR "
            f"({artifacts_dir}) 아래에 생성하세요. "
            f"다른 actor의 폴더나 /mnt/workspace 루트에 쓰지 마세요.\n"
            f"Example: write/save to '{artifacts_dir}/report.pdf'\n"
            "\n"
            "산출물 생성 후 반드시 artifact-share ``share_artifact``"
            f"(filepath='{artifacts_dir}/파일명' 또는 'artifacts/파일명', "
            f"actor_id=\"{aid}\")를 호출하고 CloudFront URL을 사용자에게 전달하세요. "
            "로컬 경로만 알려주지 마세요.\n"
            "knowledge-base ``retrieve``와 artifact-share ``share_artifact``를 "
            f"호출할 때 actor_id 인자에는 반드시 위 값(\"{aid}\")을 그대로 사용하세요. "
            "닉네임·표시 이름·추측 값으로 바꾸지 마세요.\n"
        )
    return [{"text": text}]


def run_harness(
    prompt,
    notification_queue=None,
    skill_list=None,
    mcp_servers=None,
    runtime_session_id=None,
    actor_id=None,
    files=None,
):
    """
    Run the provisioned AgentCore Harness (deployment/test_invoke_harness.py shape).
    Uses ``HARNESS_ARN`` from ``config.json`` when set; otherwise resolves ARN via
    ``bedrock-agentcore-control``: optional ``harnessName``, else ``projectName``
    (same rules as create_harness). If only one harness exists in the region,
    it is used when the name does not match (see logs).

    skill_list / mcp_servers override harness defaults for this invocation when provided.
    files: optional CloudFront/S3 image URLs attached in the chat UI.
    actor_id: account login id (cookie); used as InvokeHarness actorId and embedded
    in systemPrompt for RAG/S3 MCP tools.
    """
    tool_info_list.clear()
    tool_result_list.clear()
    tool_name_list.clear()
    if notification_queue is not None:
        notification_queue.reset()

    references = []
    image_url = []

    harness_arn = config.get("HARNESS_ARN")
    if not harness_arn:
        harness_arn = resolve_harness_arn_from_config(config, bedrock_region)
    logger.info(f"HARNESS_ARN: {harness_arn}")

    if not harness_arn:
        return (
            "Error: Could not resolve HARNESS_ARN. Check projectName and region, "
            "or set HARNESS_ARN in application/config.json after running installer.py.",
            [],
        )

    # Prefer per-call session (React task id); fall back to module default.
    session_id = (runtime_session_id or "").strip() or globals()["runtime_session_id"]
    # Keep actor_id unchanged (must match KB owner / S3 key). Only sanitize the
    # projectName fallback for ActorId character constraints.
    resolved_actor = (actor_id or "").strip()
    if not resolved_actor:
        resolved_actor = (projectName or "harness").replace("-", "_")
    effective_prompt = build_harness_prompt_with_files(prompt, files)
    system_prompt = build_harness_system_prompt(
        (actor_id or "").strip() or None
    )

    try:
        import skill as skill_mod
        import mcp_config

        boto_config = Config(
            read_timeout=300,
            connect_timeout=60,
            retries={"max_attempts": 0},
        )
        client = boto3.client(
            "bedrock-agentcore",
            region_name=bedrock_region,
            config=boto_config,
        )

        skills = skill_mod.build_harness_skills(
            skill_list or [],
            user_id=(actor_id or "").strip() or None,
        )
        tools = mcp_config.build_harness_tools(mcp_servers or [])

        import chat as chat_mod

        model_cfg = chat_mod.harness_model_config()
        logger.info(f"invoke_harness skills: {skills}")
        logger.info(f"invoke_harness tools: {tools}")
        logger.info(f"invoke_harness model: {model_cfg}")
        logger.info(f"invoke_harness actor_id: {resolved_actor}")
        logger.debug(
            f"invoke_harness: harnessArn: {harness_arn}, session: {session_id}, "
            f"actorId: {resolved_actor}, prompt_len: {len(effective_prompt or '')}, "
            f"files: {len(files or [])}"
        )

        invoke_kwargs = {
            "harnessArn": harness_arn,
            "runtimeSessionId": session_id,
            "actorId": resolved_actor,
            "model": model_cfg,
            "systemPrompt": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": effective_prompt}],
                }
            ],
        }
        if skills:
            invoke_kwargs["skills"] = skills
        if tools:
            invoke_kwargs["tools"] = tools

        response = client.invoke_harness(**invoke_kwargs)

        logger.debug(
            f"invoke_harness: response_keys: {list(response.keys())}"
        )

        current = ""
        last_usage: dict = {}
        last_metrics: dict = {}
        last_stop_reason = None

        # Per messageStart / content block: map stream events to tool UI slots (like SSE path).
        block_tool_use: dict[int, tuple[str, str]] = {}
        block_tool_result: dict[int, str] = {}
        tool_input_buffers: dict[str, str] = {}
        tool_result_buffers: dict[str, str] = {}

        stream = response.get("stream")
        if stream is None:
            logger.error(
                f"invoke_harness: no stream, response: {_json_preview(response)}"
            )
            return "Error: empty Harness response.", []

        event_index = 0
        try:
            for event in stream:
                event_index += 1
                top_keys = list(event.keys())
                logger.debug(
                    f"invoke_harness: stream_event: #{event_index}, keys: {top_keys}"
                )
                if len(top_keys) != 1:
                    logger.warning(
                        f"invoke_harness: stream_event: #{event_index}, "
                        f"expected_single_key: false, payload: {_json_preview(event, 4000)}"
                    )

                if "messageStart" in event:
                    ms = event["messageStart"]
                    block_tool_use.clear()
                    block_tool_result.clear()
                    tool_input_buffers.clear()
                    tool_result_buffers.clear()
                    logger.debug(
                        f"messageStart: role: {ms.get('role')}, full: {_json_preview(ms, 1200)}"
                    )

                elif "contentBlockStart" in event:
                    cbs = event["contentBlockStart"]
                    idx = cbs.get("contentBlockIndex")
                    start = cbs.get("start") or {}
                    if notification_queue is not None and start:
                        if "toolUse" in start:
                            tu = start["toolUse"] or {}
                            tid = (tu.get("toolUseId") or "").strip()
                            name = tu.get("name") or ""
                            ttype = tu.get("type")
                            sname = tu.get("serverName")
                            if tid and idx is not None:
                                current = on_tool_use_started(
                                    notification_queue, current, tid, tool_info_list
                                )
                                tool_name_list[tid] = name
                                if hasattr(notification_queue, "register_tool"):
                                    notification_queue.register_tool(tid, name)
                                block_tool_use[idx] = (tid, name)
                                tool_input_buffers[tid] = ""
                                extra = ""
                                if ttype:
                                    extra += f" ({ttype})"
                                if sname:
                                    extra += f" server={sname}"
                                tool_slot_update(
                                    notification_queue,
                                    f"{tid}:input",
                                    f"Tool: {name}{extra}, Input: …",
                                )
                                logger.info(
                                    f"[tool] {name}, toolUseId: {tid}, type: {ttype}, server: {sname}"
                                )
                        if "toolResult" in start:
                            tr = start["toolResult"] or {}
                            tid = (tr.get("toolUseId") or "").strip()
                            status = tr.get("status")
                            if tid and idx is not None:
                                block_tool_result[idx] = tid
                                tool_result_buffers[tid] = ""
                                tlabel = tool_name_list.get(tid, tid)
                                tool_slot_update(
                                    notification_queue,
                                    f"{tid}:result",
                                    f"Tool Result ({tlabel}): …"
                                    + (f" [{status}]" if status else ""),
                                )
                                logger.info(
                                    f"[tool_result_start] toolUseId: {tid}, name: {tlabel}, status: {status}"
                                )
                    logger.debug(
                        f"contentBlockStart: contentBlockIndex: {cbs.get('contentBlockIndex')}, "
                        f"full: {_json_preview(cbs, 3200)}"
                    )

                elif "contentBlockDelta" in event:
                    cbd = event["contentBlockDelta"]
                    idx = cbd.get("contentBlockIndex")
                    delta = cbd.get("delta") or {}
                    dkeys = list(delta.keys())
                    if "text" in delta:
                        piece = delta["text"] or ""
                        logger.info("%s", piece)
                        current += piece
                        update_streaming_result(notification_queue, current)
                    if [k for k in dkeys if k != "text"]:
                        logger.debug(
                            f"contentBlockDelta: contentBlockIndex: {idx}, delta_keys: {dkeys}"
                        )
                    if "toolUse" in delta:
                        tu = delta["toolUse"] or {}
                        tin = tu.get("input")
                        if isinstance(tin, str) and tin and idx is not None:
                            pair = block_tool_use.get(idx)
                            if pair:
                                tid, name = pair
                                tool_input_buffers[tid] = (
                                    tool_input_buffers.get(tid, "") + tin
                                )
                                if notification_queue is not None:
                                    tool_slot_update(
                                        notification_queue,
                                        f"{tid}:input",
                                        f"Tool: {name}, Input: {tool_input_buffers[tid]}",
                                    )
                            else:
                                logger.debug(
                                    f"[tool_input_delta] contentBlockIndex: {idx}, "
                                    f"fragment: {_json_preview(tin, 1600)}"
                                )
                    if "toolResult" in delta:
                        tr_part = delta.get("toolResult")
                        if tr_part is not None:
                            tid = (
                                block_tool_result.get(idx)
                                if idx is not None
                                else None
                            )
                            if tid:
                                buf = tool_result_buffers.get(tid, "")
                                if isinstance(tr_part, list):
                                    for item in tr_part:
                                        if isinstance(item, dict):
                                            if "text" in item:
                                                buf += item.get("text") or ""
                                            elif "json" in item:
                                                buf += _json_preview(
                                                    item.get("json"), 8000
                                                )
                                        else:
                                            buf += str(item)
                                else:
                                    buf += _json_preview(tr_part, 8000)
                                tool_result_buffers[tid] = buf
                                tname = tool_name_list.get(tid, "")
                                if notification_queue is not None:
                                    tool_slot_update(
                                        notification_queue,
                                        f"{tid}:result",
                                        f"Tool Result ({tname}): {buf}",
                                    )
                                content, urls, refs = get_tool_info(tname, buf)
                                if refs:
                                    for r in refs:
                                        references.append(r)
                                if urls:
                                    for url in urls:
                                        image_url.append(url)
                    if "reasoningContent" in delta:
                        rc = delta["reasoningContent"]
                        if isinstance(rc, dict):
                            logger.debug(
                                f"contentBlockDelta: reasoningContent: keys: {list(rc.keys())}, "
                                f"full: {_json_preview(rc, 2400)}"
                            )
                        else:
                            logger.debug(
                                f"contentBlockDelta: reasoningContent: {_json_preview(rc, 1200)}"
                            )

                elif "contentBlockStop" in event:
                    cbe = event["contentBlockStop"]
                    idx = cbe.get("contentBlockIndex")
                    logger.debug(
                        f"contentBlockStop: contentBlockIndex: {idx}, "
                        f"full: {_json_preview(cbe, 800)}"
                    )
                    # Log final tool input / result once when the block completes
                    if idx is not None:
                        pair = block_tool_use.get(idx)
                        if pair:
                            tid, name = pair
                            final_input = tool_input_buffers.get(tid, "")
                            logger.info(
                                f"[tool_input] {name}, toolUseId: {tid}, "
                                f"input: {_json_preview(final_input, 4000)}"
                            )
                        tid = block_tool_result.get(idx)
                        if tid:
                            tname = tool_name_list.get(tid, "")
                            buf = tool_result_buffers.get(tid, "")
                            logger.info(
                                f"[tool_result] {tname}, toolUseId: {tid}, "
                                f"body: {_json_preview(buf, 5000)}"
                            )
                            content, _, _ = get_tool_info(tname, buf)
                            if content:
                                logger.info(
                                    f"tool_result: parsed_content_len: {len(content)}"
                                )

                elif "messageStop" in event:
                    ms = event["messageStop"]
                    last_stop_reason = ms.get("stopReason")
                    logger.debug(
                        f"messageStop: stopReason: {last_stop_reason}, "
                        f"full: {_json_preview(ms, 800)}"
                    )

                elif "metadata" in event:
                    meta = event["metadata"]
                    usage = meta.get("usage") or {}
                    metrics = meta.get("metrics") or {}
                    if usage:
                        last_usage = usage
                    if metrics:
                        last_metrics = metrics
                    logger.debug(
                        f"metadata: usage: inputTokens: {usage.get('inputTokens')}, "
                        f"outputTokens: {usage.get('outputTokens')}, "
                        f"totalTokens: {usage.get('totalTokens')}, "
                        f"cacheReadInputTokens: {usage.get('cacheReadInputTokens')}, "
                        f"cacheWriteInputTokens: {usage.get('cacheWriteInputTokens')}"
                    )
                    if metrics:
                        logger.debug(
                            f"metadata: metrics: latencyMs: {metrics.get('latencyMs')}, "
                            f"full: {_json_preview(metrics, 800)}"
                        )
                    logger.debug(f"metadata: full: {_json_preview(meta, 2000)}")

                elif "internalServerException" in event:
                    exc = event["internalServerException"]
                    logger.error(
                        f"internalServerException: {_json_preview(exc, 1600)}"
                    )

                elif "validationException" in event:
                    exc = event["validationException"]
                    logger.error(
                        f"validationException: {_json_preview(exc, 2400)}"
                    )

                elif "runtimeClientError" in event:
                    err_blob = event["runtimeClientError"]
                    msg = (
                        err_blob.get("message")
                        if isinstance(err_blob, dict)
                        else str(err_blob)
                    )
                    logger.error(
                        f"runtimeClientError: message: {msg!r}, "
                        f"full: {_json_preview(err_blob, 1600)}"
                    )
                    add_notification(notification_queue, f"Harness error: {msg}")
                    err_line = f"\n\n[Harness error]\n{msg}"
                    current = (current + err_line) if current.strip() else f"Error: {msg}"

                elif "SDK_UNKNOWN_MEMBER" in event:
                    logger.warning(
                        f"invoke_harness: SDK_UNKNOWN_MEMBER: "
                        f"{_json_preview(event['SDK_UNKNOWN_MEMBER'], 800)}"
                    )

                else:
                    logger.warning(
                        f"invoke_harness: stream_event: #{event_index}, unhandled: "
                        f"{_json_preview(event, 4000)}"
                    )

            logger.debug(f"invoke_harness: stream_done: events: {event_index}")

        except EventStreamError as e:
            error_msg = str(e)
            logger.error(f"Harness: stream failure: {error_msg}")
            return f"Error: {error_msg}", []

        result = current

        if references:
            ref = "\n\n### Reference\n"
            for i, reference in enumerate(references):
                ref += (
                    f"{i + 1}. [{reference['title']}]({reference['url']}), "
                    f"{reference['content']}...\n"
                )
            result += ref

        if notification_queue is not None:
            notification_queue.result(result)

        logger.info(
            f"invoke_harness: done: events: {event_index}, "
            f"stopReason: {last_stop_reason}, "
            f"result_len: {len(result) if isinstance(result, str) else -1}, "
            f"inputTokens: {last_usage.get('inputTokens')}, "
            f"outputTokens: {last_usage.get('outputTokens')}, "
            f"totalTokens: {last_usage.get('totalTokens')}, "
            f"cacheReadInputTokens: {last_usage.get('cacheReadInputTokens')}, "
            f"cacheWriteInputTokens: {last_usage.get('cacheWriteInputTokens')}, "
            f"latencyMs: {last_metrics.get('latencyMs')}"
        )
        logger.info(f"invoke_harness: final_result:\n{result}")
        return result, image_url

    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(error_msg)
        return f"Error: {error_msg}", []
