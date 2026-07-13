# -*- coding: utf-8 -*-
#
# SOP 文档分块（字符级）
#
from chat.sop_vector.html_to_md import html_to_markdown


def build_document_text(
    title: str,
    key_word: str,
    description_md: str,
    cs_content_md: str,
) -> str:
    parts: list[str] = []
    if title and title.strip():
        parts.append(f"标题：{title.strip()}")
    if key_word and key_word.strip():
        parts.append(f"关键词：{key_word.strip()}")
    if description_md and description_md.strip():
        parts.append(f"故障现象：{description_md.strip()}")
    if cs_content_md and cs_content_md.strip():
        parts.append(f"排障话术：\n{cs_content_md.strip()}")
    return "\n\n".join(parts)


def build_sop_document(
    title: str,
    key_word: str,
    description_html: str,
    cs_content_html: str,
) -> str:
    description_md = html_to_markdown(description_html or "")
    cs_content_md = html_to_markdown(cs_content_html or "")
    return build_document_text(title, key_word, description_md, cs_content_md)


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 100) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if chunk_size <= 0:
        return [text]
    if overlap >= chunk_size:
        overlap = max(0, chunk_size // 5)

    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_size, text_len)
        if end < text_len:
            # 剩余不足一个 chunk 时并入当前块，避免产生过小的尾块
            if text_len - end < chunk_size:
                end = text_len
            else:
                split_at = _find_split_pos(text, start, end)
                if split_at > start:
                    end = split_at
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= text_len:
            break
        start = max(end - overlap, start + 1)
    return chunks


def build_chunked_documents(
    title: str,
    key_word: str,
    description_html: str,
    cs_content_html: str,
    chunk_size: int = 500,
    overlap: int = 100,
) -> list[str]:
    full_text = build_sop_document(title, key_word, description_html, cs_content_html)
    if not full_text.strip():
        return []

    chunks = chunk_text(full_text, chunk_size=chunk_size, overlap=overlap)
    if len(chunks) <= 1:
        return chunks

    header = "\n".join(
        part for part in (
            f"标题：{title.strip()}" if title and title.strip() else "",
            f"关键词：{key_word.strip()}" if key_word and key_word.strip() else "",
        ) if part
    )
    if not header:
        return chunks

    enriched = [chunks[0]]
    for chunk in chunks[1:]:
        enriched.append(f"{header}\n\n{chunk}")
    return enriched


def _find_split_pos(text: str, start: int, end: int) -> int:
    window = text[start:end]
    for sep in ("\n\n", "\n", "。", "；", ". ", "; "):
        pos = window.rfind(sep)
        if pos > len(window) * 0.4:
            return start + pos + len(sep)
    return end
