import os
import re
import subprocess
import time
import json
import threading
import uuid
from textwrap import dedent
from pathlib import Path
from pprint import pprint

import fcntl

try:
    import readline
    # #143 UTF-8 backspace fix for macOS libedit
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

WORKDIR = Path.cwd()
PROJECT_DIR = WORKDIR / ".miniCode"
PROJECT_DIR.mkdir(parents=True, exist_ok=True)
client = Anthropic(
    base_url=os.getenv("ANTHROPIC_BASE_URL"), 
    api_key=os.getenv("ANTHROPIC_AUTH_TOKEN")
)
MODEL = os.environ["MODEL_ID"]
SKILLS_DIR = WORKDIR / ".minicode" / "skills"
THRESHOLD = 10000
TRANSCRIPT_DIR = WORKDIR / ".minicode" / "transcripts"
KEEP_RECENT = 3
TASKS_DIR = WORKDIR / ".minicode" / "tasks"
TEAM_DIR = WORKDIR / ".minicode" / "team"
INBOX_DIR = TEAM_DIR / "inbox"
TAKS_CLAIM_DIR = TEAM_DIR / "tasks"

POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# -- Request trackers: correlate by request_id --
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()
_claim_lock = threading.Lock()


def estimate_tokens(messages: list) -> int:
    """Rough token count: ~4 chars per token."""
    return len(str(messages)) // 4


def detect_git_repo_root(cwd: Path) -> Path | None:
    """Return git repo root if cwd is inside a repo, else None."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return None
        root = Path(r.stdout.strip())
        return root if root.exists() else None
    except Exception:
        return None



REPO_ROOT = detect_git_repo_root(WORKDIR) or WORKDIR


# -- Layer 1: micro_compact - replace old tool results with placeholders --
def micro_compact(messages: list) -> list:
    # Collect (msg_index, part_index, tool_result_dict) for all tool_result entries
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))
    if len(tool_results) <= KEEP_RECENT:
        return messages
    # Find tool_name for each result by matching tool_use_id in prior assistant messages
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name
    # Clear old results (keep at least KEEP_RECENT)
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clear:
        if isinstance(result.get("content"), str) and len(result["content"]) > 100:
            tool_id = result.get("tool_use_id", "")
            tool_name = tool_name_map.get(tool_id, "unknown")
            result["content"] = f"[Previous: used {tool_name}]"
    return messages


# -- Layer 2: auto_compact - save transcript, summarize, replace messages --
def auto_compact(messages: list) -> list:
    # Save full transcript to disk
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    print(f"[transcript saved: {transcript_path}]")
    # Ask LLM to summarize
    coversation_text = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content":
                   "Summarize this conversation for continuity. Include: "
                   "1) What was accomplished, 2) Current state, 3) Key decisions made. "
                   "Be concise but preserve critical details.\n\n" + coversation_text}],
        max_tokens=2000,
    )
    summary = response.content[0].text
    # Replace all messages with compressed summary
    return [
        {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
        {"role": "assistant", "content": "Understood. I have the context from the summary. Continuing."},
    ]


# -- EventBus: append-only lifecycle events for observability --
class EventBus:
    def __init__(self, event_log_path: Path):
        self.path = event_log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("")
    
    def emit(
        self,
        event: str,
        task: dict | None = None,
        worktree: dict | None = None,
        error: str | None = None,
    ):
        payload = {
            "event": event,
            "ts": time.time(),
            "task": task or {},
            "worktree": worktree or {},
        }
        if error:
            payload["error"] = error
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    
    def list_recent(self, limit: int = 20) -> str:
        n = max(1, min(int(limit or 20), 200))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        recent = lines[-n:]
        items = []
        for line in recent:
            try:
                items.append(json.loads(line))
            except Exception:
                items.append({"event": "parse_error", "raw": line})
        return json.dumps(items, indent=2, ensure_ascii=False)


# -- MessageBus: JSONL inbox per teammate --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)
    
    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invaild type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(msg, ensure_ascii=False))
        return f"Sent {msg_type} to {to}"
    
    def read_inbox(self, name: str) -> list:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        with open(inbox_path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            for line in f:
                line = line.strip()
                if line:
                    messages.append(json.loads(line))
            f.seek(0)
            f.truncate()
        return messages
    
    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates."


# -- TeammateManager: persistent named agents with config.json --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}
    
    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}
    
    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False))
    
    def _find_member(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None
    
    def _set_status(self, name: str, status: str):
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()
    
    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        thread = threading.Thread(
            target=self._loop,
            args=(name, role, prompt),
            daemon=True
        )
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"
    
    def _loop(self, name: str, role: str, prompt: str):
        team_name = self.config["team_name"]
        sys_prompt = dedent(f"""
            You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}.
            Use send_message to communicate. Complete your task.
            Use idle tool when you have no more work. You will auto-claim new tasks.
            Submit plans via plan_approval before major work.
            Respond to shutdown_request with shutdown_response.
        """)
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()
        
        while True:
            # -- WORK PHASE: standard agent loop --
            for i in range(50):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg, ensure_ascii=False)})
                try:
                    response = client.messages.create(
                        model=MODEL, 
                        system=sys_prompt, 
                        messages=messages,
                        tools=tools, 
                        max_tokens=8000,
                    )
                except Exception:
                    self._set_status(name, "idle")
                    return
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                idle_requested = False
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            idle_requested = True
                            output = "Entering idle phase. Will poll for new tasks."
                        else:
                            output = self._exec(name, block.name, block.input)
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output),
                        })
                messages.append({"role": "user", "content": results})
                if idle_requested:
                    break
            
            # -- IDLE PHASE: poll for inbox messages and unclaimed tasks --
            self._set_status(name, "idle")
            resume = False
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            for _ in range(polls):
                time.sleep(POLL_INTERVAL)
                inbox = BUS.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                unclaimed = scan_unclaimed_tasks()
                if unclaimed:
                    task = unclaimed[0]
                    claim_task(task["id"], name)
                    task_prompt = dedent(f"""
                        <auto-claimed>Task #{task['id']}: {task['subject']}
                        {task.get('description', '')}</auto-claimed>
                    """)
                    if len(messages) <= 3:
                        messages.insert(0, make_identity_block(name, role, team_name))
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                    messages.append({"role": "user", "content": task_prompt})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")
    
    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        if tool_name == "bash":
            return run_bash(args["command"])
        if tool_name == "read_file":
            return run_read(args["path"])
        if tool_name == "write_file":
            return run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2, ensure_ascii=False)
        if tool_name == "shutdown_response":
            req_id = args["request_id"]
            approve = args["approve"]
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": approve},
            )
            return f"Shutdown {'approve' if approve else 'rejected'}"
        if tool_name == "plan_approval":
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            return f"Plan submitted (request_id={req_id}). Waiting for lead approval."
        if tool_name == "claim_task":
            return claim_task(args["task_id"], sender)
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        return [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message to a teammate.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "shutdown_response", "description": "Respond to a shutdown request. Approve to shut down, reject to keep working.",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            {"name": "plan_approval", "description": "Submit a plan for lead approval. Provide plan text.",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
            {"name": "idle", "description": "Signal that you have no more work. Enters idle polling phase.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "Claim a task from the task board by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}: {m['status']})")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


# -- BackgroundManager: threaded execution + notification queue --
class BackgroundManager:
    def __init__(self):
        self.tasks = {} # task_id -> {status, result, command}
        self._notification_queue = []
        self._lock = threading.Lock()
    
    def run(self, command: str) -> str:
        """Start a background thread, return task_id immediately."""
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        return f"Background task {task_id} started: {command[:80]}"
    
    def _execute(self, task_id: str, command: str):
        """Thread target: run subprogress, capture output, push to queue."""
        try:
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
            status = "error"
        except Exception as e:
            output = f"Error: {e}"
            status = "error"
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(no output)"
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": (output or "(no output)")[:500],
            })
    
    def check(self, task_id: str = None) -> str:
        """Check status of one task or list all."""
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"Error: Unknown task {task_id}"
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"
        lines = []
        for tid, t in self.tasks.items():
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
        return "\n".join(lines) if lines else "No background tasks."
    
    def drain_notifications(self) -> list:
        """Return and clear all pending completion notifications."""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs


# -- TaskManager: CRUD with dependency graph, persisted as JSON files --
class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1
    
    def _max_id(self) -> int:
        try:
            ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
            return max(ids) if ids else 0
        except Exception:
            return 0
    
    def _path(self, task_id: int) -> Path:
        return self.dir / f"task_{task_id}.json"
    
    def _load(self, task_id: int) -> dict:
        path = self._path(task_id)
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())
    
    def _save(self, task: dict):
        self._path(task["id"]).write_text(json.dumps(task, indent=2, ensure_ascii=False))

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id, 
            "subject": subject, 
            "description": description,
            "status": "pending", 
            "blockedBy": [], 
            "blocks": [], 
            "owner": "",
            "worktree": "",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2, ensure_ascii=False)
    
    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)
    
    def exists(self, task_id: int) -> bool:
        return self._path(task_id).exists()
    
    def update(self, 
               task_id: int, 
               status: str = None,
               add_block_by: list = None, 
               add_blocks: list = None,
               owner: str = None,
    ) -> str:
        task = self._load(task_id)
        if status:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            # When a task is completed, remove it from all other task's blockedBy
            if status == "completed":
                self._clear_dependency(task_id)
        if add_block_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_block_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
            # Bidirectional: also update the blocked task's blockedBy lists
            for blocked_id in add_blocks:
                try:
                    blocked = self._load(blocked_id)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
                except ValueError:
                    pass
        if owner is not None:
            task["owner"] = owner
        task["update_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)
    
    def _clear_dependency(self, completed_id: int):
        """Remove completed_id from all other task's blockedBy lists."""
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)
    
    def bind_worktree(self, task_id: int, worktree: str, owner: str = "") -> str:
        task = self._load(task_id)
        task["worktree"] = worktree
        if owner:
            task["owner"] = owner
        if task["status"] == "pending":
            task["status"] = "in_progress"
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)
    
    def unbind_worktree(self, task_id: int) -> str:
        task = self._load(task_id)
        task["worktree"] = ""
        task["update_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)
    
    def list_all(self) -> str:
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text()))
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            marker = {
                "pending": "[ ]", 
                "in_progress": "[>]", 
                "completed": "[x]",
            }.get(t["status"], "[?]")
            owner = f" owner={t['owner']}" if t.get("owner") else ""
            worktree = f" worktree={t['worktree']}" if t.get("worktree") else ""
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']} {blocked}{owner}{worktree}")
        return "\n".join(lines)


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self._load_all()
    
    def _load_all(self):
        if not self.skills_dir.exists():
            return
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text()
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}
    
    def _parse_frontmatter(self, text: str) -> tuple:
        """Parse YAML frontmatter between --- delimiters."""
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        meta = {}
        for line in match.group(1).strip().splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                meta[key.strip()] = val.strip()
        return meta, match.group(2).strip()
    
    def get_descriptions(self) -> str:
        """Layer 1: short descriptions for the system prompt."""
        if not self.skills:
            return "(no skills available)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)
    
    def get_content(self, name: str) -> str:
        """Layer 2: full skill body returned in tool_result."""
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"


class TodoManager:
    def __init__(self):
        self.items = []
    
    def update(self, items: list) -> str:
        pprint(items)
        if len(items) > 20:
            raise ValueError("Max 20 todos allowed.")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"Item {item_id}: text required.")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time.")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


# -- WorktreeManager: create/list/run/remove git worktrees + lifecycle index --
class WorktreeManager:
    def __init__(self, repo_root: Path, tasks: TaskManager, events: EventBus):
        self.repo_root = repo_root
        self.tasks = tasks
        self.events = events
        self.dir = repo_root / ".worktrees"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        if not self.index_path.exists():
            self.index_path.write_text(json.dumps({"worktrees": []}, indent=2, ensure_ascii=False))
        self.git_available = self._is_git_repo()
    
    def _is_git_repo(self) -> bool:
        try:
            r = subprocess.run(
                # ["git", "rev-parse", "--is-inside-work-tree"],
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _run_git(self, args: list[str]) -> str:
        if not self.git_available:
            raise RuntimeError("Not in a git repository. worktree tools require git.")
        r = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            msg = (r.stdout + r.stderr).strip()
            raise RuntimeError(msg or f"git {' '.join(args)} failed.")
        return (r.stdout + r.stderr).strip() or "(no output)"

    def _load_index(self) -> dict:
        return json.loads(self.index_path.read_text())

    def _save_index(self, data: dict):
        self.index_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def _find(self, name: str) -> dict | None:
        idx = self._load_index()
        for worktree in idx.get("worktrees", []):
            if worktree.get("name") == name:
                return worktree
        return None

    def _validate_name(self, name: str):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):
            raise ValueError(
                "Invalid worktree name. Use 1-40 chars: letters, numbers, ., _, -"
            )

    def create(self, name: str, task_id: int = None, base_ref: str = "HEAD") -> str:
        self._validate_name(name)
        if self._find(name):
            raise ValueError(f"Worktree '{name}' already exists in index")
        if task_id is not None and not self.tasks.exists(task_id):
            raise ValueError(f"Task {task_id} not found")
        path = self.dir / name
        branch = f"worktree/{name}"
        self.events.emit(
            "worktree.create.before",
            task={"id": task_id} if task_id is not None else {},
            worktree={"name": name, "base_ref": base_ref},
        )
        try:
            self._run_git(["worktree", "add", "-b", branch, str(path), base_ref])
            entry = {
                "name": name,
                "path": str(path),
                "branch": branch,
                "task_id": task_id,
                "status": "active",
                "created_at": time.time(),
            }
            idx = self._load_index()
            idx["worktrees"].append(entry)
            self._save_index(idx)
            if task_id is not None:
                self.tasks.bind_worktree(task_id, name)
            self.events.emit(
                "worktree.create.after",
                task={"id": task_id} if task_id is not None else {},
                worktree={
                    "name": name,
                    "path": str(path),
                    "branch": branch,
                    "status": "active",
                },
            )
            return json.dumps(entry, indent=2)
        except Exception as e:
            self.events.emit(
                "worktree.create.failed",
                task={"id": task_id} if task_id is not None else {},
                worktree={"name": name, "base_ref": base_ref},
                error=str(e),
            )
            raise

    def list_all(self) -> str:
        idx = self._load_index()
        worktrees = idx.get("worktrees", [])
        if not worktrees:
            return "No worktrees in index."
        lines = []
        for worktree in worktrees:
            suffix = f" task={worktree['task_id']}" if worktree.get("task_id") else ""
            lines.append(
                f"[{worktree.get('status', 'unknown')}] {worktree['name']} -> "
                f"{worktree['path']} ({worktree.get('branch', '-')}){suffix}"
            )
        return "\n".join(lines)

    def status(self, name: str) -> str:
        worktree = self._find(name)
        if not worktree:
            return f"Error: Unknown worktree '{name}'"
        path = Path(worktree["path"])
        if not path.exists():
            return f"Error: Worktree path missing: {path}"
        r = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        text = (r.stdout + r.stderr).strip()
        return text or "Clean worktree"

    def run(self, name: str, command: str) -> str:
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            return "Error: Dangerous command blocked"
        worktree = self._find(name)
        if not worktree:
            return f"Error: Unknown worktree '{name}'"
        path = Path(worktree["path"])
        if not path.exists():
            return f"Error: Worktree path missing: {path}"
        try:
            r = subprocess.run(
                command,
                shell=True,
                cwd=path,
                capture_output=True,
                text=True,
                timeout=300,
            )
            out = (r.stdout + r.stderr).strip()
            return out[:50000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: Timeout (300s)"

    def remove(self, name: str, force: bool = False, complete_task: bool = False) -> str:
        worktree = self._find(name)
        if not worktree:
            return f"Error: Unknown worktree '{name}'"
        self.events.emit(
            "worktree.remove.before",
            task={"id": worktree.get("task_id")} if worktree.get("task_id") is not None else {},
            worktree={"name": name, "path": worktree.get("path")},
        )
        try:
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(worktree["path"])
            self._run_git(args)
            if complete_task and worktree.get("task_id") is not None:
                task_id = worktree["task_id"]
                before = json.loads(self.tasks.get(task_id))
                self.tasks.update(task_id, status="completed")
                self.tasks.unbind_worktree(task_id)
                self.events.emit(
                    "task.completed",
                    task={
                        "id": task_id,
                        "subject": before.get("subject", ""),
                        "status": "completed",
                    },
                    worktree={"name": name},
                )
            idx = self._load_index()
            for item in idx.get("worktrees", []):
                if item.get("name") == name:
                    item["status"] = "removed"
                    item["removed_at"] = time.time()
            self._save_index(idx)
            self.events.emit(
                "worktree.remove.after",
                task={"id": worktree.get("task_id")} if worktree.get("task_id") is not None else {},
                worktree={"name": name, "path": worktree.get("path"), "status": "removed"},
            )
            return f"Removed worktree '{name}'"
        except Exception as e:
            self.events.emit(
                "worktree.remove.failed",
                task={"id": worktree.get("task_id")} if worktree.get("task_id") is not None else {},
                worktree={"name": name, "path": worktree.get("path")},
                error=str(e),
            )
            raise

    def keep(self, name: str) -> str:
        worktree = self._find(name)
        if not worktree:
            return f"Error: Unknown worktree '{name}'"
        idx = self._load_index()
        kept = None
        for item in idx.get("worktrees", []):
            if item.get("name") == name:
                item["status"] = "kept"
                item["kept_at"] = time.time()
                kept = item
        self._save_index(idx)
        self.events.emit(
            "worktree.keep",
            task={"id": worktree.get("task_id")} if worktree.get("task_id") is not None else {},
            worktree={
                "name": name,
                "path": worktree.get("path"),
                "status": "kept",
            },
        )
        return json.dumps(kept, indent=2, ensure_ascii=False) if kept else f"Error: Unknown worktree '{name}'"


# -- Task board scanning --
def scan_unclaimed_tasks() -> list:
    TAKS_CLAIM_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TAKS_CLAIM_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
            and not task.get("owner")
            and not task.get("blockedBy")):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id: int, owner: str) -> str:
    with _claim_lock:
        path = TAKS_CLAIM_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"Error: Task {task_id} not found."
        task = json.loads(path.read_text())
        task["owner"] = owner
        task["status"] = "in_progress"
        path.write_text(json.dumps(task, indent=2))
    return f"Claimed task #{task_id} for {owner}."


# -- Identity re-injection after compression --
def make_identity_block(name: str, role: str, team_name: str) -> dict:
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


TEAM = TeammateManager(TEAM_DIR)
BUS = MessageBus(INBOX_DIR)
BG = BackgroundManager()
TASKS = TaskManager(TASKS_DIR)
SKILL_LOADER = SkillLoader(SKILLS_DIR)
TODO = TodoManager()
EVENTS = EventBus(REPO_ROOT / ".worktrees" / "event.jsonl")
WORKTREES = WorktreeManager(REPO_ROOT, TASKS, EVENTS)


SYSTEM = dedent(f"""
    You are a coding agent at {WORKDIR}.
    Use load_skill to access specialized knowledge before tackling unfamiliar topics.
    Use background_run for long-running commands.
    Use the subagent tool to delegate exploration or subtasks.
    Use task tools to plan and track work.
    Use task + worktree tools for multi-task work.
    For parallel or risky changes: create tasks, allocate worktree lanes, run commands in those lanes, then choose keep/remove for closeout.
    Use worktree_events when you need lifecycle visibility.

    You can also take on the role of a team leader, spawn teammates, and communicate with them via inboxes.
    Manage teammates with shutdown and plan approval protocols.
    Teammates are autonomous -- they find work themselves.

    Skills available:
    {SKILL_LOADER.get_descriptions()}
""")
SUBAGENT_SYSTEM = dedent(f"""
    You are a coding agent at {WORKDIR}.
    Use load_skill to access specialized knowledge before tackling unfamiliar topics.
    Use background_run for long-running commands.
    Use task tools to track work.
    Complete the given task, then summarize your findings.

    Skills available:
    {SKILL_LOADER.get_descriptions()}
""")


def safe_content(content):
    return content or "(empty)"


def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escape workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked."
    try:
        print(f"\033[33m$ {command}\033[0m")
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)."


def run_read(path: str, limit: int = None) -> str:
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- Lead-specific protocol handlers --
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lean", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback}
    )
    return f"Plan {req['status']} for '{req['from']}'"


def _check_shutdown_status(request_id: str) -> str:
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))


TOOL_HANDLERS = {
    "bash":               lambda **kw: run_bash(kw["command"]),
    "read_file":          lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":         lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":          lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":               lambda **kw: TODO.update(kw["items"]),
    "load_skill":         lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    "compact":            lambda **kw: "Manual compression requested.",
    "task_create":        lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    "task_update":        lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("addBlockedBy"), kw.get("addBlocks")),
    "task_list":          lambda **kw: TASKS.list_all(),
    "task_get":           lambda **kw: TASKS.get(kw["task_id"]),
    "background_run":     lambda **kw: BG.run(kw["command"]),
    "check_background":   lambda **kw: BG.check(kw.get("task_id")),
    "spawn_teammate":     lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":     lambda **kw: TEAM.list_all(),
    "send_message":       lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":         lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2, ensure_ascii=False),
    "broadcast":          lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request":   lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response":  lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval":      lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":               lambda **kw: "Lead does not idle.",
    "claim_task":         lambda **kw: claim_task(kw["task_id"], "lead"),
    "task_bind_worktree": lambda **kw: TASKS.bind_worktree(kw["task_id"], kw["worktree"], kw.get("owner", "")),
    "worktree_create":    lambda **kw: WORKTREES.create(kw["name"], kw.get("task_id"), kw.get("base_ref", "HEAD")),
    "worktree_list":      lambda **kw: WORKTREES.list_all(),
    "worktree_status":    lambda **kw: WORKTREES.status(kw["name"]),
    "worktree_run":       lambda **kw: WORKTREES.run(kw["name"], kw["command"]),
    "worktree_keep":      lambda **kw: WORKTREES.keep(kw["name"]),
    "worktree_remove":    lambda **kw: WORKTREES.remove(kw["name"], kw.get("force", False), kw.get("complete_task", False)),
    "worktree_events":    lambda **kw: EVENTS.list_recent(kw.get("limit", 20)),
}


CHILD_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {
         "type": "object",
         "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["path"]
    }},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {
         "type": "object", 
         "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, 
         "required": ["path", "content"]
    }},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {
         "type": "object", 
         "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, 
         "required": ["path", "old_text", "new_text"]
    }},
    {"name": "todo", "description": "Update task list. Track progress on multi-step tasks.",
     "input_schema": {
         "type": "object",
         "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "text", "status"]}}}, 
         "required": ["items"]
    }},
    {"name": "load_skill", "description": "Load specialized knowledge by name.",
     "input_schema": {
         "type": "object", 
         "properties": {"name": {"type": "string", "description": "Skill name to load"}},
         "required": ["name"]
    }},
    {"name": "task_update", "description": "Update a task's status or dependencies.",
     "input_schema": {
         "type": "object", 
         "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "addBlocks": {"type": "array", "items": {"type": "integer"}}}, 
         "required": ["task_id"]
    }},
    {"name": "task_list", "description": "List all tasks with status summary.",
     "input_schema": {
         "type": "object", 
         "properties": {}
    }},
    {"name": "task_get", "description": "Get full details of a task by ID.",
     "input_schema": {
         "type": "object", 
         "properties": {"task_id": {"type": "integer"}}, 
         "required": ["task_id"]
    }},
]

PARENT_TOOLS = CHILD_TOOLS + [
    {"name": "subagent", "description": "Spwan a subagent with fresh context. It shares the filesystem but not conversation history.",
     "input_schema": {
         "type": "object", 
         "properties": {"prompt": {"type": "string"}, "desription": {"type": "string", "description": "Short description of the task"}},
         "required": ["prompt"]
    }},
    {"name": "compact", "description": "Trigger manual conversation compression.",
     "input_schema": {
         "type": "object",
         "properties": {"focus": {"type": "string", "description": "What to preserve in the summary"}}
    }},
    {"name": "task_create", "description": "Create a new task.",
     "input_schema": {
         "type": "object", 
         "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, 
         "required": ["subject"]
    }},
    {"name": "background_run", "description": "Run command in background thread. Returns task_id immediately.",
     "input_schema": {
         "type": "object", 
         "properties": {"command": {"type": "string"}}, 
         "required": ["command"]
    }},
    {"name": "check_background", "description": "Check background task status. Omit task_id to list all.",
     "input_schema": {
         "type": "object", 
         "properties": {"task_id": {"type": "string"}}
    }},
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate that runs in its own thread.",
     "input_schema": {
         "type": "object", 
         "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, 
         "required": ["name", "role", "prompt"]
    }},
    {"name": "list_teammates", "description": "List all teammates with name, role, status.",
     "input_schema": {
         "type": "object", 
         "properties": {}
    }},
    {"name": "send_message", "description": "Send a message to a teammate's inbox.",
     "input_schema": {
         "type": "object", 
         "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, 
         "required": ["to", "content"]
    }},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {
         "type": "object", 
         "properties": {}
    }},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {
         "type": "object", 
         "properties": {"content": {"type": "string"}}, 
         "required": ["content"]
    }},
    {"name": "shutdown_request", "description": "Request a teammate to shut down gracefully. Returns a request_id for tracking.",
     "input_schema": {
         "type": "object", 
         "properties": {"teammate": {"type": "string"}}, 
         "required": ["teammate"]
    }},
    {"name": "shutdown_response", "description": "Check the status of a shutdown request by request_id.",
     "input_schema": {
         "type": "object", 
         "properties": {"request_id": {"type": "string"}}, 
         "required": ["request_id"]
    }},
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan. Provide request_id + approve + optional feedback.",
     "input_schema": {
         "type": "object", 
         "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, 
         "required": ["request_id", "approve"]
    }},
    {"name": "idle", "description": "Enter idle state (for lead -- rarely used).",
     "input_schema": {
         "type": "object", 
         "properties": {}
    }},
    {"name": "claim_task", "description": "Claim a task from the board by ID.",
     "input_schema": {
         "type": "object", 
         "properties": {"task_id": {"type": "integer"}}, 
         "required": ["task_id"]
    }},
    {"name": "task_bind_worktree", "description": "Bind a task to a worktree name.",
     "input_schema": {
         "type": "object",
         "properties": {"task_id": {"type": "integer"}, "worktree": {"type": "string"}, "owner": {"type": "string"}},
         "required": ["task_id", "worktree"],
    }},
    {
        "name": "worktree_create",
        "description": "Create a git worktree and optionally bind it to a task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "task_id": {"type": "integer"},
                "base_ref": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "worktree_list",
        "description": "List worktrees tracked in .worktrees/index.json.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "worktree_status",
        "description": "Show git status for one worktree.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "worktree_run",
        "description": "Run a shell command in a named worktree directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "command": {"type": "string"},
            },
            "required": ["name", "command"],
        },
    },
    {
        "name": "worktree_remove",
        "description": "Remove a worktree and optionally mark its bound task completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "force": {"type": "boolean"},
                "complete_task": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "worktree_keep",
        "description": "Mark a worktree as kept in lifecycle state without removing it.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "worktree_events",
        "description": "List recent worktree/task lifecycle events from .worktrees/events.jsonl.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
]


# -- Subagent: fresh context, filtered tools, summary-only return --
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    for _ in range(30): # safety limit
        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_messages,
            tools=CHILD_TOOLS, max_tokens=8000,
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        sub_messages.append({"role": "user", "content": safe_content(results)})
    # Only the final text returns to the parent -- child context is discarded
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"


def agent_loop(messages: list):
    round_since_todo = 0
    while True:
        # Layer 1: micro_compact before each LLM call
        micro_compact(messages)
        # Layer 2: auto_compact if token estimate exceeds threshold
        if estimate_tokens(messages) > THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = auto_compact(messages)
        
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2, ensure_ascii=False)}</inbox>"
            })
            messages.append({
                "role": "assistant",
                "content": "Noted inbox messages."
            })

        # TODO: 如果处于 tool_use loop, 这里会构造出 assis/tool_use - user/tool-result - user/bg-result - assis/noted 这样的序列，会影响后续 tool use 吗？而且因为只要不是 tool use，loop 直接 return，所以如果判断 agent state 是 TOOL_WAIT/NORMAL 的话，实际上只有在进入 loop 时才会加上 bg-res
        # Drain background notifications and inject as system message before LLM call
        notifs = BG.drain_notifications()
        if notifs and messages:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
            messages.append({"role": "assistant", "content": "Noted background results."})
        
        response = client.messages.create(
            model=MODEL, 
            system=SYSTEM, 
            messages=messages,
            tools=PARENT_TOOLS, 
            max_tokens=8000,
        )
        # Append assistant turn
        messages.append({"role": "assistant", "content": safe_content(response.content)})
        # If the model don't call a tool, we're done
        if response.stop_reason != "tool_use":
            return
        # Execute each tool call, collect results
        results = []
        used_todo = False
        manual_compact = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "subagent":
                    desc = block.input.get("description", "subagent")
                    print(f"* subagent ({desc}): {block.input['prompt'][:80]}")
                    output = run_subagent(block.input["prompt"])
                elif block.name == "compact":
                    manual_compact = True
                    output = "Compressing..."
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"
                print(f"* {block.name}\n{output[:800]}")
                results.append({
                    "type": "tool_result", 
                    "tool_use_id": block.id, 
                    "content": output
                })
        #         if block.name == "todo":
        #             used_todo = True
        # # Nag reminder is injected below, alongside tool results
        # round_since_todo = 0 if used_todo else round_since_todo + 1
        # if round_since_todo >= 3:
        #     results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})
        messages.append({"role": "user", "content": safe_content(results)})
        # Layer 3: manual compact triggered by the compact tool
        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages)

if __name__ == "__main__":
    if not WORKTREES.git_available:
        print("Note: Not in a git repo. worktree_* tools will return errors.")
    
    history = []
    while True:
        try:
            query = input("\033[36mminiCode >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2, ensure_ascii=False))
            continue
        if query.strip() == "/tasks":
            TAKS_CLAIM_DIR.mkdir(exist_ok=True)
            for f in sorted(TAKS_CLAIM_DIR.glob("task_*.json")):
                t = json.loads(f.read_text())
                marker = {
                    "pending": "[ ]", 
                    "in_progress": "[>]", 
                    "completed": "[x]",
                }.get(t["status"], "[?]")
                owner = f"@{t['owner']}" if t.get("owner") else ""
                print(f"  {marker} #{t['id']}: {t['subject']} {owner}")
            continue
        history.append({"role": "user", "content": safe_content(query)})
        try:
            agent_loop(history)
        except Exception as e:
            print(f"ERROR: {e}\n")
            pprint(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
