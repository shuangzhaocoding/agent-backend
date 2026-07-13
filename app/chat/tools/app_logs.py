# -*- coding: utf-8 -*-
#
# APP 日志下载、过滤打包并上传 OBS（逻辑对齐 app_module/router.py get_app_logs）
#
from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
import traceback
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp

from async_outer_apis import HuaweiOBSClient, OpenAIOT, OpenWhale
from common import config_file
from common.logger import logger

DEFAULT_APP_LOG_LOOKBACK_DAYS = 7
PREVIEW_MAX_CHARS = 2000
MAX_LINES_PER_OUTPUT_FILE = 1000
OUTPUT_FILE_INDEX_WIDTH = 3
OBS_APP_LOG_PREFIX = "agent/app_logs"

_DATETIME_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{6})")
_FILTER_STRINGS = frozenset({
    "| ScanResult ]",
    "store.narwal.com/mall/customPage/getByPageKeyword",
    "APP商城首页_中国_中文",
    "GetAllReducedMaps_Response",
    "get_all_reduced_maps",
    "RESPONSE_PAYLOAD:GetMap_Response",
    "robotTrajectoryFormat",
})


def _normalize_region(region: str | None) -> str:
    value = (region or "cn").strip().lower()
    return value if value in {"cn", "us"} else "cn"


def _parse_log_datetime(value: str | None, default: datetime, *, end_of_day: bool = False) -> datetime:
    text = (value or "").strip()
    if not text:
        return default
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                if end_of_day:
                    return parsed.replace(hour=23, minute=59, second=59)
                return parsed.replace(hour=0, minute=0, second=0)
            return parsed
        except ValueError:
            continue
    return default


def _build_obs_object_key(user_account: str) -> str:
    date_part = datetime.now().strftime("%Y-%m-%d")
    safe_account = re.sub(r"[^\w.-]+", "_", user_account.strip()) or "unknown"
    return f"{OBS_APP_LOG_PREFIX}/{date_part}/{uuid.uuid4().hex}/{safe_account}_app_logs.zip"


async def _upload_zip_to_obs(zip_path: str, object_key: str) -> dict[str, Any]:
    client = HuaweiOBSClient()
    try:
        upload_resp = await client.upload_file(
            object_key=object_key,
            file_path=zip_path,
            public_read=True,
        )
    finally:
        client.close()

    if not upload_resp.get("success"):
        return {"error": upload_resp.get("error") or "APP 日志上传 OBS 失败"}

    data = upload_resp.get("data") or {}
    url = data.get("url")
    if not url:
        return {"error": "OBS 上传成功但未返回访问链接"}

    return {
        "obs_key": data.get("key") or object_key,
        "download_url": url,
    }


async def _download_app_log(download_log_dir: str, filename: str, fileurl: str) -> None:
    download_path = os.path.join(download_log_dir, filename)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    timeout = aiohttp.ClientTimeout(total=300, connect=60)
    connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        try:
            logger.debug(f"开始下载 APP 日志: {fileurl}")
            async with session.get(fileurl, headers=headers) as response:
                if response.status != 200:
                    logger.error(f"APP 日志下载失败 status={response.status} url={fileurl}")
                    return
                async with aiofiles.open(download_path, "wb") as handle:
                    while True:
                        chunk = await response.content.read(8192)
                        if not chunk:
                            break
                        await handle.write(chunk)
        except asyncio.TimeoutError:
            logger.error(f"APP 日志下载超时: {fileurl}")
        except Exception:
            logger.error(f"APP 日志下载异常: {traceback.format_exc()}")


async def _process_log_file(src_file: str) -> list[str]:
    try:
        lines: list[str] = []
        async with aiofiles.open(src_file, "r", encoding="utf-8") as handle:
            async for line in handle:
                if not any(filter_str in line for filter_str in _FILTER_STRINGS):
                    lines.append(line)
        return lines
    except UnicodeDecodeError:
        logger.error(f"APP 日志文件编码错误，跳过：{src_file}")
        return []


async def _rename_file_by_datetime(src_file: str, dest_dir: str) -> str:
    import tempfile

    if not os.path.exists(src_file):
        logger.error(f"源文件不存在：{src_file}")
        return src_file

    os.makedirs(dest_dir, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
        shutil.copy2(src_file, temp_path)
        async with aiofiles.open(temp_path, "r", encoding="utf-8") as handle:
            chunk = await handle.read(4096)
        match = _DATETIME_PATTERN.search(chunk)
        if not match:
            logger.warning(f"未找到日期时间格式：{src_file}")
            os.unlink(temp_path)
            return src_file

        dt_fmt_fn = (
            match.group(1)
            .replace(" ", "_")
            .replace(":", "-")
            .replace("/", "_")
            .replace("\\", "_")
            .replace(".", "_")
        )
        dest_file = os.path.join(dest_dir, dt_fmt_fn)
        if os.path.exists(dest_file):
            base, ext = os.path.splitext(dt_fmt_fn)
            counter = 1
            while os.path.exists(os.path.join(dest_dir, f"{base}_{counter}{ext}")):
                counter += 1
            dest_file = os.path.join(dest_dir, f"{base}_{counter}{ext}")

        shutil.move(temp_path, dest_file)
        if os.path.exists(src_file):
            try:
                os.unlink(src_file)
            except OSError as exc:
                logger.warning(f"无法删除原始文件（但移动已成功）：{src_file}，错误：{exc}")
        return dest_file
    except Exception as exc:
        logger.error(f"重命名 APP 日志文件失败：{src_file}，错误：{exc}")
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
        return src_file


async def _process_zip_file(
    filename: str,
    extrated_dir: str,
    extrated_rename_dir: str,
    return_list_dir: str,
) -> None:
    logger.debug(f"压缩包文件名：{filename}")
    try:
        with zipfile.ZipFile(filename, "r") as archive:
            extract_target = f"{extrated_dir}/{uuid.uuid4().hex}"
            logger.debug(f"{filename}，压缩包内文件：{archive.namelist()}")
            logger.debug(f"{filename}，解压至：{extract_target}")
            archive.extractall(extract_target)
            Path(extrated_rename_dir).mkdir(parents=True, exist_ok=True)

            rename_tasks = []
            for member in archive.namelist():
                src_file = f"{extract_target}/{member}"
                if os.path.isfile(src_file):
                    rename_tasks.append(_rename_file_by_datetime(src_file, extrated_rename_dir))
            renamed_files = await asyncio.gather(*rename_tasks)

            files = sorted(path for path in renamed_files if os.path.isfile(path))
            filter_tasks = [_process_log_file(file_path) for file_path in files]
            results = await asyncio.gather(*filter_tasks)

            current_lines: list[str] = []
            file_index = 0
            for lines in results:
                current_lines.extend(lines)
                while len(current_lines) >= MAX_LINES_PER_OUTPUT_FILE:
                    fn_name = format(file_index, f"0{OUTPUT_FILE_INDEX_WIDTH}")
                    output_file = f"{return_list_dir}/{fn_name}"
                    async with aiofiles.open(output_file, "w", encoding="utf-8") as handle:
                        await handle.writelines(current_lines[:MAX_LINES_PER_OUTPUT_FILE])
                    current_lines = current_lines[MAX_LINES_PER_OUTPUT_FILE:]
                    file_index += 1

            if current_lines:
                fn_name = format(file_index, f"0{OUTPUT_FILE_INDEX_WIDTH}")
                output_file = f"{return_list_dir}/{fn_name}"
                async with aiofiles.open(output_file, "w", encoding="utf-8") as handle:
                    await handle.writelines(current_lines)
    except zipfile.BadZipFile:
        logger.error(traceback.format_exc())
        logger.error(f"疑似压缩文件损坏，跳过此压缩包解压：{filename}")


async def _create_download_zip(app_log_dir: str, user_account: str) -> tuple[str, int]:
    """对齐 app_module download=True：打包 rename_to 目录下的日志文件。"""
    zip_file_path = os.path.join(app_log_dir, f"{user_account}.zip")
    log_files_dir = os.path.join(app_log_dir, "rename_to")
    if not os.path.isdir(log_files_dir):
        return "", 0

    proc = await asyncio.create_subprocess_shell(
        f"cat {log_files_dir}/* > {log_files_dir}/{user_account}.log ",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    log_files = glob.glob(os.path.join(log_files_dir, "*"))
    if not log_files:
        return "", 0

    with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in log_files:
            if os.path.isfile(file_path):
                archive.write(file_path, arcname=os.path.basename(file_path))

    return zip_file_path, len(log_files)


def _read_preview(return_list_dir: str, limit: int = PREVIEW_MAX_CHARS) -> str:
    log_files = sorted(glob.glob(os.path.join(return_list_dir, "*")))
    if not log_files:
        return ""

    try:
        with open(log_files[0], "r", encoding="utf-8") as handle:
            content = handle.read(limit + 1)
    except (OSError, UnicodeDecodeError):
        return ""

    if len(content) > limit:
        return content[:limit] + "\n[...完整日志请通过下载链接获取...]"
    return content


async def fetch_app_logs(
    user_account: str,
    *,
    region: str = "cn",
    start_time: str = "",
    end_time: str = "",
) -> dict[str, Any]:
    account = (user_account or "").strip()
    if not account:
        return {"error": "请提供 APP 用户 ID（8 位 uid）或用户 uuid（32 位）"}

    norm_region = _normalize_region(region)
    now = datetime.now()
    st_dt = _parse_log_datetime(
        start_time,
        (now - timedelta(days=DEFAULT_APP_LOG_LOOKBACK_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ),
    )
    ed_text = (end_time or "").strip()
    ed_default = now
    ed_end_of_day = bool(ed_text and " " not in ed_text and len(ed_text) == 10)
    ed_dt = _parse_log_datetime(end_time, ed_default, end_of_day=ed_end_of_day)

    user_info = await OpenWhale(site=norm_region).get_user_info(account)
    if not user_info.get("result"):
        return {
            "user_account": account,
            "region": norm_region,
            "error": f"帐号 {account} 不存在",
        }

    user_uuid = user_info["data"]["uuid"]
    st_time_str = st_dt.strftime("%Y-%m-%d %H:%M:%S")
    ed_time_str = ed_dt.strftime("%Y-%m-%d %H:%M:%S")
    app_log_urls = await OpenAIOT(country=norm_region).get_app_logs(
        user_uuid,
        st_time_str,
        ed_time_str,
    )

    app_log_dir = os.path.join(config_file.tmp_files_path, account + str(uuid.uuid4()))
    download_zip_dir = os.path.join(app_log_dir, "download")
    extrated_rename_dir = os.path.join(app_log_dir, "rename_to")
    return_list_dir = os.path.join(app_log_dir, "list")
    extrated_dir = os.path.join(app_log_dir, "extract")

    try:
        os.makedirs(extrated_rename_dir, exist_ok=True)
        os.makedirs(download_zip_dir, exist_ok=True)
        os.makedirs(return_list_dir, exist_ok=True)
        os.makedirs(extrated_dir, exist_ok=True)

        downloaded = 0
        for item in app_log_urls or []:
            file_name = str(item.get("fileName") or "")
            logger.debug(f"日志文件名：{file_name}")
            if not file_name.startswith("App"):
                continue
            await _download_app_log(
                download_zip_dir,
                f"{item.get('id')}.zip",
                str(item.get("filePath") or ""),
            )
            downloaded += 1

        download_files = sorted(glob.glob(os.path.join(download_zip_dir, "*")))
        zip_tasks = []
        for filename in download_files:
            if filename.endswith(".zip"):
                zip_tasks.append(
                    _process_zip_file(
                        filename,
                        extrated_dir,
                        extrated_rename_dir,
                        return_list_dir,
                    )
                )
        if zip_tasks:
            await asyncio.gather(*zip_tasks)

        result_files = glob.glob(os.path.join(return_list_dir, "*"))
        if not result_files:
            return {
                "user_account": account,
                "region": norm_region,
                "user_uuid": user_uuid,
                "start_time": st_time_str,
                "end_time": ed_time_str,
                "downloaded_archives": downloaded,
                "matched": False,
                "error": "查询时间段内无 APP 日志",
            }

        export_name = f"{account}.zip"
        export_zip_path, file_count = await _create_download_zip(app_log_dir, account)
        if file_count <= 0 or not export_zip_path:
            return {
                "user_account": account,
                "region": norm_region,
                "error": "APP 日志打包失败",
            }

        object_key = _build_obs_object_key(account)
        upload_result = await _upload_zip_to_obs(export_zip_path, object_key)
        if upload_result.get("error"):
            return {
                "user_account": account,
                "region": norm_region,
                "error": upload_result["error"],
            }

        preview = _read_preview(return_list_dir)
        download_url = upload_result["download_url"]

        return {
            "user_account": account,
            "region": norm_region,
            "user_uuid": user_uuid,
            "start_time": st_time_str,
            "end_time": ed_time_str,
            "downloaded_archives": downloaded,
            "matched": True,
            "chunk_count": len(result_files),
            "file_count": file_count,
            "file_name": export_name,
            "obs_key": upload_result["obs_key"],
            "download_url": download_url,
            "preview": preview,
            "message": f"APP 日志已打包并上传，请点击下载：[{export_name}]({download_url})",
        }
    finally:
        if os.path.isdir(app_log_dir):
            shutil.rmtree(app_log_dir, ignore_errors=True)
