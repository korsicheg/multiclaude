"""A formatted Telegram message (rich_message: headings, lists, code, tables) as text, for the
router when it polls Telegram itself (updates.py). markdown() gives GitHub-flavored Markdown and
plain_text() the bare words of a message replied to, both as bin/patch-telegram-plugin's
TypeScript does for the front desk's plugin (multiclaudeRichMarkdown, multiclaudePlainText):
keep the two alike."""

MARKS = {"bold": "**", "italic": "*", "strikethrough": "~~", "spoiler": "||"}
MEDIA = ("photo", "video", "animation", "audio", "document", "voice_note")


def markdown(blocks):
    """A rich message's blocks as Markdown. Photos, files and other media inside it become a note
    with their file_id, for `tg download`."""
    return "\n\n".join(filter(None, (block_markdown(block) for block in blocks or [])))


def block_markdown(block):
    kind = block.get("type")
    if kind in ("paragraph", "footer"):
        return text_markdown(block.get("text"))
    if kind == "heading":
        size = block.get("size")
        return f"{'#' * (2 if size is None else size)} {text_markdown(block.get('text'))}"
    if kind == "pre":
        return "\n".join(["```" + (block.get("language") or ""), unformatted(block.get("text")), "```"])
    if kind == "divider":
        return "---"
    if kind == "mathematical_expression":
        return f"$${block.get('expression')}$$"
    if kind == "list":
        return "\n".join(list_item_markdown(item) for item in block.get("items") or [])
    if kind == "blockquote":
        return quote(markdown(block.get("blocks")), block.get("credit"))
    if kind in ("expandable_blockquote", "pullquote"):
        return quote(text_markdown(block.get("text")), block.get("credit"))
    if kind == "table":
        return "\n\n".join(filter(None, [table_markdown(block.get("cells")), text_markdown(block.get("caption"))]))
    if kind == "details":
        return (f"<details><summary>{text_markdown(block.get('summary'))}</summary>\n\n"
                f"{markdown(block.get('blocks'))}\n\n</details>")
    if kind == "anchor":
        return ""
    return embed_markdown(block)


def text_markdown(text):
    """Rich text (a string, a list of pieces, or one formatted piece) as inline Markdown."""
    if text is None or isinstance(text, str):
        return text or ""
    if isinstance(text, list):
        return "".join(text_markdown(piece) for piece in text)
    kind, inner = text.get("type"), text_markdown(text.get("text"))
    if kind in MARKS:
        return MARKS[kind] + inner + MARKS[kind]
    if kind == "code":
        return "`" + unformatted(text.get("text")) + "`"
    if kind == "url":
        return inner if inner == text.get("url") else f"[{inner}]({text.get('url')})"
    if kind == "mathematical_expression":
        return f"${text.get('expression')}$"
    if kind == "custom_emoji":
        return text.get("alternative_text") or ""
    if kind == "button":
        return f"[{(text.get('button') or {}).get('text')}]"
    return inner


def unformatted(text):
    """Rich text with its formatting left out, for inside code."""
    if text is None or isinstance(text, str):
        return text or ""
    if isinstance(text, list):
        return "".join(unformatted(piece) for piece in text)
    for key in ("text", "alternative_text", "expression"):
        if text.get(key) is not None:
            return unformatted(text[key])
    return ""


def list_item_markdown(item):
    if item.get("has_checkbox"):
        label = f"- [{'x' if item.get('is_checked') else ' '}]"
    else:
        label = f"{item['value']}." if item.get("value") is not None else "-"
    first, *rest = "\n".join(filter(None, (block_markdown(block) for block in item.get("blocks") or []))).split("\n")
    indent = " " * (len(label) + 1)
    return "\n".join([f"{label} {first}", *(line and indent + line for line in rest)])


def quote(quoted, credit):
    by = text_markdown(credit)
    whole = "\n\n".join(filter(None, [quoted, by and f"— {by}"]))
    return "\n".join(f"> {line}".rstrip() for line in whole.split("\n"))


def table_markdown(rows):
    if not rows:
        return ""
    lines = ["| " + " | ".join(text_markdown(cell.get("text")).replace("|", "\\|").replace("\n", " ") for cell in row) + " |"
             for row in rows]
    return "\n".join([lines[0], "|" + " --- |" * len(rows[0]), *lines[1:]])


def embed_markdown(block):
    """Media, maps and buttons: what the block holds, with its caption. A photo or file keeps its
    file_id; a photo comes in several sizes, the largest last."""
    kind, media = str(block.get("type")), None
    if kind in MEDIA and block.get(kind):
        media = block[kind][-1] if isinstance(block[kind], list) else block[kind]
    place = block.get("location") and f"{block['location'].get('latitude')}, {block['location'].get('longitude')}"
    name = (media or {}).get("file_name") or place
    file_id = f" · file_id: {media['file_id']}" if media and media.get("file_id") else ""
    what = f"({kind.replace('_', ' ')}{f': {name}' if name else ''}{file_id})"
    buttons = " ".join(f"[{button.get('text')}]" for button in block.get("buttons") or [])
    caption = text_markdown((block.get("caption") or {}).get("text"))
    return "\n\n".join(filter(None, [what, markdown(block.get("blocks")), buttons, caption]))


def plain_text(value):
    """The plain text of a rich message (its blocks, list items, table rows), or of a string."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(plain_text(piece) for piece in value)
    if not isinstance(value, dict):
        return ""
    words = [plain_text(value[key]) for key in ("summary", "text", "expression", "cells") if key in value]
    lines = [plain_text(piece) for key in ("blocks", "items", "rows") if isinstance(value.get(key), list)
             for piece in value[key]]
    return "\n".join(filter(None, words + lines))
