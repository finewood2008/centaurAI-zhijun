"""MindOS 导入校验规则（P1 阶段 / 本期开放 Excel 与 PPT）。

文档/图片与音频已开放：文档/图片 ≤50MB，音频（MP3/WAV/M4A）≤200MB。
文档含 Excel（.xlsx/.xlsm/.xls）与 PPT（.pptx）；旧版 .ppt 二进制格式暂不支持。
前端与后端必须使用同一份白名单与大小限制，避免仅依赖浏览器限制。
所有返回均不暴露宿主机物理路径。
"""
from pathlib import Path

# ---- MindOS 白名单 ----
DOC_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".xlsx", ".xlsm", ".xls", ".pptx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a"}

# ---- 大小限制 ----
DOC_IMAGE_MAX_BYTES = 50 * 1024 * 1024  # 文档/图片上限 50MB
AUDIO_MAX_BYTES = 200 * 1024 * 1024     # 音频上限 200MB

# ---- 校验结果状态 ----
OK = "ok"
OVERSIZE = "oversize"
UNSUPPORTED = "unsupported"
AUDIO_PENDING = "audio_pending"  # 保留常量以兼容旧调用方，P13 后不再返回

# ---- 类型分类 ----
CATEGORY_DOCUMENT = "document"
CATEGORY_IMAGE = "image"
CATEGORY_AUDIO = "audio"
CATEGORY_UNKNOWN = "unknown"


def file_category(ext: str) -> str:
    ext = ext.lower()
    if ext in DOC_EXTENSIONS:
        return CATEGORY_DOCUMENT
    if ext in IMAGE_EXTENSIONS:
        return CATEGORY_IMAGE
    if ext in AUDIO_EXTENSIONS:
        return CATEGORY_AUDIO
    return CATEGORY_UNKNOWN


def validate_import(filename: str, size: int) -> dict:
    """校验单个待导入文件，返回 {status, category, message}。

    - ok：允许加入待上传队列（“待上传”）
    - oversize：文档/图片超过 50MB，音频超过 200MB
    - unsupported：不支持的文件类型
    """
    ext = Path(filename).suffix.lower()
    category = file_category(ext)

    if category == CATEGORY_UNKNOWN:
        return {
            "status": UNSUPPORTED,
            "category": category,
            "message": "不支持的文件类型",
        }
    max_bytes = AUDIO_MAX_BYTES if category == CATEGORY_AUDIO else DOC_IMAGE_MAX_BYTES
    if size > max_bytes:
        limit_mb = "200MB" if category == CATEGORY_AUDIO else "50MB"
        return {
            "status": OVERSIZE,
            "category": category,
            "message": f"文件超过 {limit_mb} 限制",
        }
    return {
        "status": OK,
        "category": category,
        "message": "待上传",
    }
