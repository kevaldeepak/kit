"""Discord text helpers."""
DISCORD_SAFE_LIMIT = 1900

def split_chunks(text, limit=DISCORD_SAFE_LIMIT):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks
