import ast
import datetime
import operator
import os

import chromadb
import streamlit as st
from google import genai
from google.genai import errors, types
from pypdf import PdfReader

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
CHROMA_PATH = os.getenv("CHROMA_PATH", "/data/chroma")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

st.set_page_config(page_title="Mini Agentic RAG", page_icon="🤖")
st.title("🤖 Mini Agentic RAG (Gemini)")

if APP_PASSWORD and not st.session_state.get("authed"):
    pw = st.text_input("Password", type="password")
    if pw and pw == APP_PASSWORD:
        st.session_state.authed = True
        st.rerun()
    st.stop()

if not os.getenv("GEMINI_API_KEY"):
    st.error("GEMINI_API_KEY is not set.")
    st.stop()


@st.cache_resource
def get_collection():
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    return client.get_or_create_collection("docs")


@st.cache_resource
def get_llm():
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


collection = get_collection()
llm = get_llm()


def read_file(f) -> str:
    if f.name.lower().endswith(".pdf"):
        return "\n".join(page.extract_text() or "" for page in PdfReader(f).pages)
    return f.getvalue().decode("utf-8", errors="ignore")


def chunk(text: str, size: int = 800, overlap: int = 150) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size - overlap
    return chunks


def ingest(f) -> int:
    pieces = [c for c in chunk(read_file(f)) if c.strip()]
    if not pieces:
        return 0
    collection.upsert(
        ids=[f"{f.name}-{i}" for i in range(len(pieces))],
        documents=pieces,
        metadatas=[{"source": f.name, "chunk": i} for i in range(len(pieces))],
    )
    return len(pieces)


def search_documents(query: str) -> str:
    n = collection.count()
    if n == 0:
        return "The knowledge base is empty. Ask the user to upload documents."
    res = collection.query(query_texts=[query], n_results=min(4, n))
    parts = [
        f"[source: {meta['source']}, chunk {meta['chunk']}]\n{doc}"
        for doc, meta in zip(res["documents"][0], res["metadatas"][0])
    ]
    return "\n\n---\n\n".join(parts)


# Arithmetic is evaluated by walking the AST rather than calling eval()
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("Unsupported expression")


def calculate(expression: str) -> str:
    return str(_eval(ast.parse(expression, mode="eval").body))


def get_current_time() -> str:
    return datetime.datetime.now().strftime("%A, %d %B %Y, %H:%M:%S")


TOOL_FUNCS = {
    "search_documents": search_documents,
    "calculate": calculate,
    "get_current_time": get_current_time,
}

TOOL_DECLARATIONS = [
    {
        "name": "search_documents",
        "description": "Semantic search over the user's uploaded documents. "
        "Use this for any question about the content of their files.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "calculate",
        "description": "Evaluate an arithmetic expression such as '2340 * 0.15'. "
        "Supports + - * / and parentheses.",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "The arithmetic expression"},
            },
            "required": ["expression"],
        },
    },
    {
        "name": "get_current_time",
        "description": "Get the current date and time on the server.",
    },
]


def run_tool(name: str, args: dict) -> str:
    try:
        return TOOL_FUNCS[name](**args)
    except Exception as e:
        return f"Tool error: {e}"


SYSTEM = """You are a helpful assistant with access to tools.
- For questions about the user's documents, ALWAYS call search_documents first
  and answer only from what it returns. Mention the source file names.
- If the documents don't contain the answer, say so instead of guessing.
- Use calculate for any arithmetic and get_current_time for date/time questions."""

CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM,
    tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)

MAX_STEPS = 6


def to_contents(history: list[dict]) -> list[types.Content]:
    return [
        types.Content(
            role="model" if m["role"] == "assistant" else "user",
            parts=[types.Part(text=m["content"])],
        )
        for m in history
    ]


def run_agent(history: list[dict], on_step) -> str:
    contents = to_contents(history)
    for _ in range(MAX_STEPS):
        resp = llm.models.generate_content(model=MODEL, contents=contents, config=CONFIG)

        calls = resp.function_calls
        if not calls:
            return resp.text or "(The model returned an empty response.)"

        # Keep the model turn as returned so thought signatures are preserved
        contents.append(resp.candidates[0].content)

        result_parts = []
        for call in calls:
            args = dict(call.args or {})
            output = run_tool(call.name, args)
            on_step(call.name, args, output)
            result_parts.append(
                types.Part.from_function_response(name=call.name, response={"result": output})
            )
        contents.append(types.Content(role="user", parts=result_parts))
    return "I stopped because I hit the maximum number of steps."


with st.sidebar:
    st.header("Knowledge base")
    files = st.file_uploader(
        "Upload .txt / .md / .pdf", type=["txt", "md", "pdf"], accept_multiple_files=True
    )
    if st.button("Index files") and files:
        for f in files:
            st.success(f"{f.name}: {ingest(f)} chunks indexed")
    st.caption(f"Chunks stored: {collection.count()}")
    st.caption(f"Model: {MODEL}")
    st.warning(
        "On Gemini's free tier, Google may use prompts and document snippets to improve "
        "its products. Don't upload private or sensitive files."
    )

if "history" not in st.session_state:
    st.session_state.history = []

for m in st.session_state.history:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

if question := st.chat_input("Ask about your documents, or try: what is 15% of 2340?"):
    st.session_state.history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        steps = st.expander("🔍 Agent steps", expanded=True)

        def on_step(name, args, output):
            steps.markdown(f"**Tool:** `{name}`  \n**Input:** `{args}`")
            steps.code(output[:600])

        try:
            with st.spinner("Thinking..."):
                answer = run_agent(st.session_state.history, on_step)
        except errors.APIError as e:
            st.session_state.history.pop()
            if e.code == 429:
                st.error("Rate limit reached. Wait a minute and try again.")
            else:
                st.error(f"Gemini API error {e.code}: {str(e)[:300]}")
            st.stop()
        st.markdown(answer)

    st.session_state.history.append({"role": "assistant", "content": answer})
