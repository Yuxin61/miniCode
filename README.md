# miniCode

This project is an implementation of https://learn.shareai.run/.

**Count Lines of Code**

```
-------------------------------------------------------------------------------
Language                     files          blank        comment           code
-------------------------------------------------------------------------------
Python                           1            131             81           1311
Markdown                         1            333              0            661
TOML                             1              0              0             10
-------------------------------------------------------------------------------
SUM:                             3            464             81           1982
-------------------------------------------------------------------------------
```

## dotenv

```properties
ANTHROPIC_BASE_URL=
ANTHROPIC_AUTH_TOKEN=
MODEL_ID=
```

## Stages

**Path**

- (Tools & Execution) s01: The Agent Loop
- (Tools & Execution) s02: Tools
- (Planning & Coordination) s03: Todo
- (Planning & Coordination) s04: Subagents
- (Planning & Coordination) s05: Skills
- (Memory Management) s06: Compact
- (Planning & Coordination) s07: Tasks
- (Concueerncy) s08: Background Tasks
- (Collaboration) s09: Agent Teams
- (Collaboration) s10: Team Protocols
- (Collaboration) s11: Autonomous Agents
- (Collaboration) s12: Worktree + Task Isolation

### s01. The Agent Loop

A language model can reason about code, but it can't touch the real world -- can't read files, run tests, or check errors. Without a loop, every tool call requires you to manually copy-paste results back. You become the loop.

---

The entire secret of an AI coding agent in one pattern:

```
while stop_reason == "tool_use":
    response = LLM(messages, tools)
    execute tools
    append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                            ^               |
                            |   tool_result |
                            +---------------+
                            (loop continues)
```

This is the core loop: feed tool results back to the model until the model decides to stop. Production agents layer policy, hooks, and lifecycle controls on top.

**Key Functions**

- Agent loop.

**Tools**

1. Bash

### s02: Tool Use

With only `bash`, the agent shells out for everything. `cat` truncates unpredictably, sed fails on special characters, and every bash call is an unconstrained security surface. Dedicated tools like `read_file` and `write_file` let you enforce path sandboxing at the tool level.

The key insight: adding tools does not require changing the loop.

---

The agent loop from s01 didn't change. We just added tools to the array and a dispatch map to route calls.

Key insight: "The loop didn't change at all. I just added tools."

```
    +----------+      +-------+      +------------------+
    |   User   | ---> |  LLM  | ---> | Tool Dispatch    |
    |  prompt  |      |       |      | {                |
    +----------+      +---+---+      |   bash: run_bash |
                        ^          |   read: run_read |
                        |          |   write: run_wr  |
                        +----------+   edit: run_edit |
                        tool_result| }                |
                                    +------------------+
```

**Tools**

2. Read File
3. Write File
4. Edit File

### s03: Todo List

On multi-step tasks, the model loses track. It repeats work, skips steps, or wanders off. Long conversations make this worse -- the system prompt fades as tool results fill the context. A 10-step refactoring might complete steps 1-3, then the model starts improvising because it forgot steps 4-10.

---

The model tracks its own progress via a TodoManager. A nag reminder forces it to keep updating when it forgets.


Key insight: "The agent can track its own progress -- and I can see it."

```
    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> | Tools   |
    |  prompt  |      |       |      | + todo  |
    +----------+      +---+---+      +----+----+
                            ^               |
                            |   tool_result |
                            +---------------+
                                |
                    +-----------+-----------+
                    | TodoManager state     |
                    | [ ] task A            |
                    | [>] task B <- doing   |
                    | [x] task C            |
                    +-----------------------+
                                |
                    if rounds_since_todo >= 3:
                        inject <reminder>
```

**Tools**

5. Todo

### s04: Subagent

As the agent works, its messages array grows. Every file read, every bash output stays in context permanently. "What testing framework does this project use?" might require reading 5 files, but the parent only needs the answer: "pytest."

---

Spawn a child agent with fresh `messages=[]`. The child works in its own context, sharing the filesystem, then returns only a summary to the parent.

Key insight: "Process isolation gives context isolation for free."

```
    Parent agent                    Subagent
    +------------------+            +------------------+
    | messages=[...]   |            | messages=[]      |  <-- fresh
    |                  |  dispatch  |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |            |   call tools     |
    |   description="" |            |   append results |
    |                  |  summary   |                  |
    |   result = "..." | <--------- | return last text |
    +------------------+            +------------------+
              |
    Parent context stays clean.
    Subagent context is discarded.
```

**Tools**

6. Task (subagent, only parent)

### s05: Skills

You want the agent to follow domain-specific workflows: git conventions, testing patterns, code review checklists. Putting everything in the system prompt wastes tokens on unused skills. 10 skills at 2000 tokens each = 20,000 tokens, most of which are irrelevant to any given task.

---

Two-layer skill injection that avoids bloating the system prompt:

- Layer 1 (cheap): skill names in system prompt (~100 tokens/skill)
- Layer 2 (on demand): full skill body in tool_result

Key insight: "Don't put everything in the system prompt. Load on demand."

```
skills/
    pdf/
    SKILL.md          <-- frontmatter (name, description) + body
    code-review/
    SKILL.md
```

```
    System prompt:
    +--------------------------------------+
    | You are a coding agent.              |
    | Skills available:                    |
    |   - pdf: Process PDF files...        |  <-- Layer 1: metadata only
    |   - code-review: Review code...      |
    +--------------------------------------+
    When model calls load_skill("pdf"):
    +--------------------------------------+
    | tool_result:                         |
    | <skill>                              |
    |   Full PDF processing instructions   |  <-- Layer 2: full body
    |   Step 1: ...                        |
    |   Step 2: ...                        |
    | </skill>                             |
    +--------------------------------------+
```

**Tools**

7. Skill

### s06: Compact

The context window is finite. A single `read_file` on a 1000-line file costs ~4000 tokens. After reading 30 files and running 20 bash commands, you hit 100,000+ tokens. The agent cannot work on large codebases without compression.

---

Context will fill up; three-layer compression strategy enables infinite sessions.

Key insight: "The agent can forget strategically and keep working forever."

```
    Every turn:
    +------------------+
    | Tool call result |
    +------------------+
            |
            v
    [Layer 1: micro_compact]        (silent, every turn)
      Replace tool_result content older than last 3
      with "[Previous: used {tool_name}]"
            |
            v
    [Check: tokens > 50000?]
       |               |
       no              yes
       |               |
       v               v
    continue    [Layer 2: auto_compact]
                  Save full transcript to .transcripts/
                  Ask LLM to summarize conversation.
                  Replace all messages with [summary].
                        |
                        v
                [Layer 3: compact tool]
                  Model calls compact -> immediate summarization.
                  Same as auto, triggered manually.
```

**Tools**

8. Compact

### s07: Task Manager

s03's TodoManager is a flat checklist in memory: no ordering, no dependencies, no status beyond done-or-not. Real goals have structure -- task B depends on task A, tasks C and D can run in parallel, task E waits for both C and D.

Without explicit relationships, the agent can't tell what's ready, what's blocked, or what can run concurrently. And because the list lives only in memory, context compression (s06) wipes it clean.

---

Tasks persist as JSON files in tasks/ so they survive context compression.

Each task has a dependency graph (blockedBy/blocks).

Key insight: "State that survives compression -- because it's outside the conversation."

```
.tasks/
    task_1.json  {"id":1, "subject":"...", "status":"completed", ...}
    task_2.json  {"id":2, "blockedBy":[1], "status":"pending", ...}
    task_3.json  {"id":3, "blockedBy":[2], "blocks":[], ...}
```

```
    Dependency resolution:
    +----------+     +----------+     +----------+
    | task 1   | --> | task 2   | --> | task 3   |
    | complete |     | blocked  |     | blocked  |
    +----------+     +----------+     +----------+
         |                ^
         +--- completing task 1 removes it from task 2's blockedBy
```

**Tools**

9. Task Create
10. Task Update
11. Task List
12. Task Get

### s08: Background Tasks

Some commands take minutes: `npm install`, `pytest`, `docker build`. With a blocking loop, the model sits idle waiting. If the user asks "install dependencies and while that runs, create the config file," the agent does them sequentially, not in parallel.

---

Run commands in background threads. A notification queue is drained before each LLM call to deliver results.

Key insight: "Fire and forget -- the agent doesn't block while the command runs."

```
    Main thread                Background thread
    +-----------------+        +-----------------+
    | agent loop      |        | task executes   |
    | ...             |        | ...             |
    | [LLM call]  <---+------- | enqueue(result) |
    |  ^drain queue   |        +-----------------+
    +-----------------+

    Timeline:
    Agent ----[spawn A]----[spawn B]----[other work]----
                 |              |
                 v              v
              [A runs]      [B runs]        (parallel)
                 |              |
                 +-- notification queue --> [results injected]
```

**Tools**

13. Background Task

### s09: Agent Teams

Subagents (s04) are disposable: spawn, work, return summary, die. No identity, no memory between invocations. Background tasks (s08) run shell commands but can't make LLM-guided decisions.

Real teamwork needs: (1) persistent agents that outlive a single prompt, (2) identity and lifecycle management, (3) a communication channel between agents.

---

Persistent named agents with file-based JSONL inboxes. Each teammate runs its own agent loop in a separate thread. Communication via append-only inboxes.

- Subagent (s04):  spawn -> execute -> return summary -> destroyed
- Teammate (s09):  spawn -> work -> idle -> work -> ... -> shutdown

Key insight: "Teammates that can talk to each other."

```
    .team/config.json                   .team/inbox/
    +----------------------------+      +------------------+
    | {"team_name": "default",   |      | alice.jsonl      |
    |  "members": [              |      | bob.jsonl        |
    |    {"name":"alice",        |      | lead.jsonl       |
    |     "role":"coder",        |      +------------------+
    |     "status":"idle"}       |
    |  ]}                        |      send_message("alice", "fix bug"):
    +----------------------------+        open("alice.jsonl", "a").write(msg)
                                        read_inbox("alice"):
    spawn_teammate("alice","coder",...)   msgs = [json.loads(l) for l in ...]
         |                                open("alice.jsonl", "w").close()
         v                                return msgs  # drain
    Thread: alice             Thread: bob
    +------------------+      +------------------+
    | agent_loop       |      | agent_loop       |
    | status: working  |      | status: idle     |
    | ... runs tools   |      | ... waits ...    |
    | status -> idle   |      |                  |
    +------------------+      +------------------+
    5 message types (all declared, not all handled here):
    +-------------------------+-----------------------------------+
    | message                 | Normal text message               |
    | broadcast               | Sent to all teammates             |
    | shutdown_request        | Request graceful shutdown (s10)   |
    | shutdown_response       | Approve/reject shutdown (s10)     |
    | plan_approval_response  | Approve/reject plan (s10)         |
    +-------------------------+-----------------------------------+
```

**Tools**

14. Spawn Teammate
15. List Teammate
16. Read Inbox
17. Broadcast

### s10: Team Protocols

In s09, teammates work and communicate but lack structured coordination:

Shutdown: Killing a thread leaves files half-written and config.json stale. You need a handshake: the lead requests, the teammate approves (finish and exit) or rejects (keep working).

Plan approval: When the lead says "refactor the auth module," the teammate starts immediately. For high-risk changes, the lead should review the plan first.

Both share the same structure: one side sends a request with a unique ID, the other responds referencing that ID.

---

Shutdown protocol and plan approval protocol, both using the same `request_id` correlation pattern. Builds on s09's team messaging.

Key insight: "Same request_id correlation pattern, two domains."

```
    Shutdown FSM: pending -> approved | rejected

    Lead                              Teammate
    +---------------------+          +---------------------+
    | shutdown_request    |          |                     |
    | {                   | -------> | receives request    |
    |   request_id: abc   |          | decides: approve?   |
    | }                   |          |                     |
    +---------------------+          +---------------------+
                                             |
    +---------------------+          +-------v-------------+
    | shutdown_response   | <------- | shutdown_response   |
    | {                   |          | {                   |
    |   request_id: abc   |          |   request_id: abc   |
    |   approve: true     |          |   approve: true     |
    | }                   |          | }                   |
    +---------------------+          +---------------------+
            |
            v
    status -> "shutdown", thread stops

    Plan approval FSM: pending -> approved | rejected

    Teammate                          Lead
    +---------------------+          +---------------------+
    | plan_approval       |          |                     |
    | submit: {plan:"..."}| -------> | reviews plan text   |
    +---------------------+          | approve/reject?     |
                                     +---------------------+
                                             |
    +---------------------+          +-------v-------------+
    | plan_approval_resp  | <------- | plan_approval       |
    | {approve: true}     |          | review: {req_id,    |
    +---------------------+          |   approve: true}    |
                                     +---------------------+
    Trackers: {request_id: {"target|from": name, "status": "pending|..."}}
```

**Tools**

18. Shutdown Request
19. Shutdown Response
20. Plan Approval

### s11: Autonomous Agents

In s09-s10, teammates only work when explicitly told to. The lead must spawn each one with a specific prompt. 10 unclaimed tasks on the board? The lead assigns each one manually. Doesn't scale.

True autonomy: teammates scan the task board themselves, claim unclaimed tasks, work on them, then look for more.

One subtlety: after context compression (s06), the agent might forget who it is. Identity re-injection fixes this.

---

Idle cycle with task board polling, auto-claiming unclaimed tasks, and identity re-injection after context compression. Builds on s10's protocols.

Key insight: "The agent finds work itself."

```
    Teammate lifecycle:
    +-------+
    | spawn |
    +---+---+
        |
        v
    +-------+  tool_use    +-------+
    | WORK  | <----------- |  LLM  |
    +---+---+              +-------+
        |
        | stop_reason != tool_use
        v
    +--------+
    | IDLE   | poll every 5s for up to 60s
    +---+----+
        |
        +---> check inbox -> message? -> resume WORK
        |
        +---> scan .tasks/ -> unclaimed? -> claim -> resume WORK
        |
        +---> timeout (60s) -> shutdown

    Identity re-injection after compression:
    messages = [identity_block, ...remaining...]
    "You are 'coder', role: backend, team: my-team"
```

**Tools**

21. Idle
22. Clain Task

### s12: Worktree + Task Isolation

By s11, agents can claim and complete tasks autonomously. But every task runs in one shared directory. Two agents refactoring different modules at the same time will collide: agent A edits `config.py`, agent B edits `config.py`, unstaged changes mix, and neither can roll back cleanly.

The task board tracks what to do but has no opinion about where to do it. The fix: give each task its own git worktree directory. Tasks manage goals, worktrees manage execution context. Bind them by task ID.

---

Directory-level isolation for parallel task execution. Tasks are the control plane and worktrees are the execution plane.

Key insight: "Isolate by directory, coordinate by task ID."

```json
    .tasks/task_12.json
      {
        "id": 12,
        "subject": "Implement auth refactor",
        "status": "in_progress",
        "worktree": "auth-refactor"
      }
    .worktrees/index.json
      {
        "worktrees": [
          {
            "name": "auth-refactor",
            "path": ".../.worktrees/auth-refactor",
            "branch": "wt/auth-refactor",
            "task_id": 12,
            "status": "active"
          }
        ]
      }
```

```
    Control plane (.tasks/)                        Execution plane (.worktrees/)
    +-----------------------------+                +----------------------------+
    | task_1.json                 |                | auth-refactor/             |
    |   status: in_progress       |    <------>    |   branch: wt/auth-refactor |
    |   worktree: "auth-refactor" |                |   task_id: 1               |
    +-----------------------------+                +----------------------------+
    | task_2.json                 |                | ui-login/                  |
    |   status: pending           |    <------>    |   branch: wt/ui-login      |
    |   worktree: "ui-login"      |                |   task_id: 2               |
    +-----------------------------+                +----------------------------+
    
    index.json   (worktree registry)
    events.jsonl (lifecycle log)

    State machines:
    Task:     pending -> in_progress -> completed
    Worktree: absent  -> active      -> removed | kept
```

**Tools**

23. Task Bind Worktree
24. Worktree Create
25. Worktree List
26. Worktree Status
27. Worktree Run
28. Worktree Keep
29. Worktree Remove
30. Worktree Events

## Tests

**s01**

1. Create a file called hello.py that prints "Hello, World!"
2. List all Python files in this directory
3. What is the current git branch?
4. Create a directory called test_output and write 3 files in it

**s02**

1. Read the file requirements.txt
2. Create a file called greet.py with a greet(name) function
3. Edit greet.py to add a docstring to the function
4. Read greet.py to verify the edit worked

**s03**

1. Create a file called hello.py with a hello(name) function, then refactor the file hello.py: add type hints, docstrings, and a main guard
2. Create a Python package with `__init__.py`, `utils.py`, and `tests/test_utils.py`
3. Review all Python files in `tests` directory and fix any style issues

**s04**

1. Use a subtask to find what third-libraries this project uses
2. Delegate: read all `.py` files and summarize what each one does
3. Use a task to create a new module, then verify it from here

**s05**

1. What skills are available?
2. Load the agent-builder skill and follow its instructions
3. I need to create a mcp server -- load the relevant skill first
4. Build an MCP server using the mcp-builder skill

**s06**

1. Read every Python file in the src/ directory one by one (Observe micro-compact replacing old results)
2. Keep reading files until compression triggers automatically
3. Use the compact tool to manually compress the conversation

**s07**

1. Create 3 tasks: "Setup project", "Write code", "Write tests". Make them depend on each other in order.
2. List all tasks and show the dependency graph
3. Complete task 1 and then list tasks to see task 2 unblocked
4. Create a task board for refactoring: parse -> transform -> emit -> test, where transform and emit can run in parallel after parse. Then, list all tasks and show the dependency graph

**s08**

1. Run "sleep 5 && echo done" in the background, then create a file while it runs
2. Start 3 background tasks: "sleep 2", "sleep 4", "sleep 6". Check their status.
3. Run pytest in the background and keep working on other things

**s09**

1. Spawn alice (coder) and bob (tester). Have alice send bob a message.
2. Broadcast "status update: phase 1 complete" to all teammates
3. Check the lead inbox for any messages
4. Type `/team` to see the team roster with statuses
5. Type `/inbox` to manually check the leader's inbox

**s10**

1. Spawn alice as a coder. Then request her shutdown.
2. List teammates to see alice's status after shutdown approval
3. Spawn bob with a risky refactoring task. Review and reject his plan.
4. Spawn charlie, have him submit a plan, then approve it.
5. Type `/team` to monitor statuses

**s11**

1. Create 3 tasks on the board, then spawn alice and bob. Watch them auto-claim.
2. Spawn a coder teammate and let it find work from the task board itself
3. Create tasks with dependencies. Watch teammates respect the blocked order.
4. Type `/tasks` to see the task board with owners
5. Type `/team` to monitor who is working vs idle

**s12**

1. Create tasks for backend auth and frontend login page, then list tasks.
2. Create worktree "auth-refactor" for task 1, then bind task 2 to a new worktree "ui-login".
3. Run "git status --short" in worktree "auth-refactor".
4. Keep worktree "ui-login", then list worktrees and inspect events.
5. Remove worktree "auth-refactor" with complete_task=true, then list tasks/worktrees/events.

## Explorer: Design Decisions

**s01: The Agent Loop**

### Why Bash Alone Is Enough

Bash can read files, write files, run arbitrary programs, pipe data between processes, and manage the filesystem. Any additional tool (read_file, write_file, etc.) would be a strict subset of what bash already provides. Adding more tools doesn't unlock new capabilities -- it just adds surface area for confusion. The model has to learn fewer tool schemas, and the implementation stays under 100 lines. This is the minimal viable agent: one tool, one loop.

**Alternatives Considered**

We could have started with a richer toolset (file I/O, HTTP, database), but that would obscure the core insight: an LLM with a shell is already a general-purpose agent. Starting minimal also makes it obvious what each subsequent version actually adds.

### Recursive Process Spawning as Subagent Mechanism

When the agent runs `python v0.py "subtask"`, it spawns a completely new process with a fresh LLM context. This child process is effectively a subagent: it has its own system prompt, its own conversation history, and its own task focus. When it finishes, the parent gets the stdout result. This is subagent delegation without any framework -- just Unix process semantics. Each child process naturally isolates concerns because it literally cannot see the parent's context.

**Alternatives Considered**

A framework-level subagent system (like v3's Task tool) gives more control over what tools the subagent can access and how results are returned. But at v0, the point is to show that process spawning is the most primitive form of agent delegation -- no shared memory, no message passing, just stdin/stdout.

### No Planning Framework -- The Model Decides

There is no planner, no task queue, no state machine. The system prompt tells the model how to approach problems, and the model decides what bash command to run next based on the conversation so far. This is intentional: at this level, adding a planning layer would be premature abstraction. The model's chain-of-thought IS the plan. The agent loop just keeps asking the model what to do until it stops requesting tools.

**Alternatives Considered**

Later versions (v2) add explicit planning via TodoWrite. But v0 proves that implicit planning through the model's reasoning is sufficient for many tasks. The planning framework only becomes necessary when you need external visibility into the agent's intentions.

**s02: Tools**

### Why Exactly Four Tools

The four tools are bash, read_file, write_file, and edit_file. Together they cover roughly 95% of coding tasks. Bash handles execution and arbitrary commands. Read_file provides precise file reading with line numbers. Write_file creates or overwrites files. Edit_file does surgical string replacement. More tools would increase the model's cognitive load -- it has to decide which tool to use, and more options means more chances of picking the wrong one. Fewer tools also means fewer tool schemas to maintain and fewer edge cases to handle.

**Alternatives Considered**

We could add specialized tools (list_directory, search_files, http_request), and later versions do. But at this stage, bash already covers those use cases. The split from v0's single tool to v1's four tools is specifically about giving the model structured I/O for file operations, where bash's quoting and escaping often trips up the model.

### The Model IS the Agent

The core agent loop is trivially simple: while True, call the LLM, if it returns tool_use blocks then execute them and feed results back, if it returns only text then stop. There is no router, no decision tree, no workflow engine. The model itself decides what to do, when to stop, and how to recover from errors. The code is just plumbing that connects the model to tools. This is a philosophical stance: agent behavior emerges from the model, not from the framework.

**Alternatives Considered**

Many agent frameworks add elaborate orchestration layers: ReAct loops with explicit Thought/Action/Observation parsing, LangChain-style chains, AutoGPT-style goal decomposition. These frameworks assume the model needs scaffolding to behave as an agent. Our approach assumes the model already knows how to be an agent -- it just needs tools to act on the world.

### JSON Schemas for Every Tool

Each tool defines a strict JSON schema for its input parameters. For example, edit_file requires old_string and new_string as exact strings, not regex patterns. This eliminates an entire class of bugs: the model can't pass malformed input because the API validates against the schema before execution. It also makes the model's intent unambiguous -- when it calls edit_file with specific strings, there's no parsing ambiguity about what it wants to change.

**Alternatives Considered**

Some agent systems let the model output free-form text that gets parsed with regex or heuristics (e.g., extracting code from markdown blocks). This is fragile -- the model might format output slightly differently and break the parser. JSON schemas trade flexibility for reliability.

**s03: Todo**

### Making Plans Visible via TodoWrite

Instead of letting the model plan silently in its chain-of-thought, we force plans to be externalized through the TodoWrite tool. Each plan item has a status (pending, in_progress, completed) that gets tracked explicitly. This has three benefits: (1) users can see what the agent intends to do before it does it, (2) developers can debug agent behavior by inspecting the plan state, (3) the agent itself can refer back to its plan in later turns when earlier context has scrolled away.

**Alternatives Considered**

The model could plan internally via chain-of-thought reasoning (as it does in v0/v1). Internal planning works but is invisible and ephemeral -- once the thinking scrolls out of context, the plan is lost. Claude's extended thinking is another option, but it's not inspectable by the user or by downstream tools.

### Only One Task Can Be In-Progress

The TodoWrite tool enforces that at most one task has status 'in_progress' at any time. If the model tries to start a second task, it must first complete or abandon the current one. This constraint prevents a subtle failure mode: models that try to 'multitask' by interleaving work on multiple items tend to lose track of state and produce half-finished results. Sequential focus produces higher quality than parallel thrashing.

**Alternatives Considered**

Allowing multiple in-progress items would let the agent context-switch between tasks, which seems more flexible. In practice, LLMs handle context-switching poorly -- they lose track of which task they were working on and mix up details between tasks. The single-focus constraint is a guardrail that improves output quality.

### Maximum of 20 Plan Items

TodoWrite caps the plan at 20 items. This is a deliberate constraint against over-planning. Models tend to decompose tasks into increasingly fine-grained steps when unconstrained, producing 50-item plans where each step is trivial. Long plans are fragile: if step 15 fails, the remaining 35 steps may all be invalid. Short plans (under 20 items) stay at the right abstraction level and are easier to adapt when reality diverges from the plan.

**Alternatives Considered**

No cap would give the model full flexibility, but in practice leads to absurdly detailed plans. A dynamic cap (proportional to task complexity) would be smarter but adds complexity. The fixed cap of 20 is a simple heuristic that works well empirically -- most real coding tasks can be expressed in 5-15 meaningful steps.

**s04: Subagents**

### Subagents Get Fresh Context, Not Shared History

When a parent agent spawns a subagent via the Task tool, the subagent starts with a clean message history containing only the system prompt and the delegated task description. It does NOT inherit the parent's conversation. This is context isolation: the subagent can focus entirely on its specific subtask without being distracted by hundreds of messages from the parent's broader conversation. The result is returned to the parent as a single tool_result, collapsing potentially dozens of subagent turns into one concise answer.

**Alternatives Considered**

Sharing the parent's full context would give the subagent more information, but it would also flood the subagent with irrelevant details. Context window is finite -- filling it with parent history leaves less room for the subagent's own work. Fork-based approaches (copy the parent context) are a middle ground but still waste tokens on irrelevant history.


### Explore Agents Cannot Write Files

When spawning a subagent with the 'Explore' type, it receives only read-only tools: bash (with restrictions), read_file, and search tools. It cannot call write_file or edit_file. This implements the principle of least privilege: an agent tasked with 'find all usages of function X' doesn't need write access. Removing write tools eliminates the risk of accidental file modification during exploration, and it also narrows the tool space so the model makes better decisions with fewer options.

**Alternatives Considered**

Giving all subagents full tool access is simpler to implement but violates least privilege. A permission-request system (subagent asks parent for write access) adds complexity and latency. Static tool filtering by role is the pragmatic middle ground -- simple to implement, effective at preventing accidents.

### Subagents Cannot Spawn Their Own Subagents

The Task tool is not included in the subagent's tool set. A subagent must complete its work directly -- it cannot delegate further. This prevents infinite delegation loops: without this constraint, an agent could spawn a subagent that spawns another subagent, each one re-delegating the same task in slightly different words, consuming tokens without making progress. One level of delegation handles the vast majority of use cases. If a task is too complex for a single subagent, the parent should decompose it differently.

**Alternatives Considered**

Allowing recursive delegation (bounded by depth) would handle deeply nested tasks but adds complexity and the risk of runaway token consumption. In practice, single-level delegation covers most real-world coding tasks. Multi-level delegation is addressed in later versions (v6+) through persistent team structures instead of recursive spawning.

**s05: Skills**

### Skills Inject via tool_result, Not System Prompt

When the agent invokes the Skill tool, the skill's content (a SKILL.md file) is returned as a tool_result in a user message, not injected into the system prompt. This is a deliberate caching optimization: the system prompt remains static across turns, which means API providers can cache it (Anthropic's prompt caching, OpenAI's system message caching). If skill content were in the system prompt, it would change every time a new skill is loaded, invalidating the cache. By putting dynamic content in tool_result, we keep the expensive system prompt cacheable while still getting skill knowledge into context.

**Alternatives Considered**

Injecting skills into the system prompt is simpler and gives skills higher priority in the model's attention. But it breaks prompt caching (every skill load creates a new system prompt variant) and bloats the system prompt over time as skills accumulate. The tool_result approach keeps things cache-friendly at the cost of slightly lower attention priority.

### On-Demand Skill Loading Instead of Upfront

Skills are not loaded at startup. The agent starts with only the skill names and descriptions (from frontmatter). When the agent decides it needs a specific skill, it calls the Skill tool, which loads the full SKILL.md body into context. This keeps the initial prompt small and focused. An agent solving a Python bug doesn't need the Kubernetes deployment skill loaded -- that would waste context window space and potentially confuse the model with irrelevant instructions.

**Alternatives Considered**

Loading all skills upfront guarantees the model always has all knowledge available, but wastes tokens on irrelevant skills and may hit context limits. A recommendation system (model suggests skills, human approves) adds latency. Lazy loading lets the model self-serve the knowledge it needs, when it needs it.

### YAML Frontmatter + Markdown Body in SKILL.md

Each SKILL.md file has two parts: YAML frontmatter (name, description, globs) and a markdown body (the actual instructions). The frontmatter serves as metadata for the skill registry -- it's what gets listed when the agent asks 'what skills are available?' The body is the payload that gets loaded on demand. This separation means you can list 100 skills (reading only frontmatter, a few bytes each) without loading 100 full instruction sets (potentially thousands of tokens each).

**Alternatives Considered**

A separate metadata file (skill.yaml + skill.md) would work but doubles the number of files. Embedding metadata in the markdown (as headings or comments) requires parsing the full file to extract metadata. Frontmatter is a well-established convention (Jekyll, Hugo, Astro) that keeps metadata and content co-located but separately parseable.

**s06: Compact**

### Three-Layer Compression Strategy

Context management uses three distinct layers, each with different cost/benefit profiles. (1) Microcompact runs every turn and is nearly free: it truncates tool_result blocks from older messages, stripping verbose command output that's no longer needed. (2) Auto_compact triggers when token count exceeds a threshold: it calls the LLM to generate a conversation summary, which is expensive but dramatically reduces context size. (3) Manual compact is user-triggered for explicit 'start fresh' moments. Layering these means the cheap operation runs constantly (keeping context tidy) while the expensive operation runs rarely (only when actually needed).

**Alternatives Considered**

A single compression strategy (e.g., always summarize at 80% capacity) would be simpler but wasteful -- most of the time, microcompact alone keeps things manageable. A sliding window (drop oldest N messages) is cheap but loses important context. The three-layer approach gives the best token efficiency: cheap cleanup constantly, expensive summarization rarely.

### MIN_SAVINGS = 20,000 Tokens Before Compressing

Auto_compact only triggers when the estimated savings (current tokens minus estimated summary size) exceed 20,000 tokens. Compression is not free: the summary itself consumes tokens, plus there's the API call cost to generate it. If the conversation is only 25,000 tokens, compressing might save 5,000 tokens but cost an API call and produce a summary that's less coherent than the original. The 20K threshold ensures compression only happens when the savings meaningfully exceed the overhead.

**Alternatives Considered**

A percentage-based threshold (compress when context is 80% full) adapts to different context window sizes but doesn't account for the fixed cost of generating a summary. A fixed threshold of 10K would compress more aggressively but often isn't worth it. The 20K value was chosen empirically: it's the point where compression savings consistently outweigh the quality loss from summarization.

### Summary Replaces ALL Messages, Not Partial History

When auto_compact fires, it generates a summary and replaces the ENTIRE message history with that summary. It does not keep the last N messages alongside the summary. This avoids a subtle coherence problem: if you keep recent messages plus a summary of older ones, the model sees two representations of overlapping content. The summary might say 'we decided to use approach X' while a recent message still shows the deliberation process, creating contradictory signals. A clean summary is a single coherent narrative.

**Alternatives Considered**

Keeping the last 5-10 messages alongside the summary preserves recent detail and gives the model more to work with. But it creates the overlap problem described above, and makes the total context size less predictable. Some systems use a 'sliding window + summary' approach which works but requires careful tuning of the overlap region.

### Full Conversation Archived to JSONL on Disk

Even though context is compressed in memory, the full uncompressed conversation is appended to a JSONL file on disk. Every message, every tool call, every result -- nothing is lost. This means compression is a lossy operation on the in-memory context but a lossless operation on the permanent record. Post-hoc analysis (debugging agent behavior, computing token usage, training data extraction) can always work from the complete transcript. The JSONL format is append-only, making it safe for concurrent writes and easy to stream-process.

**Alternatives Considered**

Not archiving saves disk space but makes debugging hard -- when the agent makes a mistake, you can't see what it was 'thinking' 200 messages ago because that context was compressed away. Database storage (SQLite) would provide queryability but adds a dependency. JSONL is the simplest format that supports append-only writes and line-by-line processing.

**s07: Tasks**

### Tasks Stored as JSON Files, Not In-Memory

Tasks are persisted as JSON files in a .tasks/ directory on the filesystem instead of being held in memory. This has three critical benefits: (1) Tasks survive process crashes -- if the agent dies mid-task, the task board is still on disk when it restarts. (2) Multiple agents can read and write to the same task directory, enabling multi-agent coordination without shared memory. (3) Humans can inspect and manually edit task files for debugging. The filesystem becomes the shared database.

**Alternatives Considered**

In-memory storage (like v2's TodoWrite) is simpler and faster but loses state on crash and doesn't work across multiple agent processes. A proper database (SQLite, Redis) would provide ACID guarantees and better concurrency, but adds a dependency and operational complexity. Files are the zero-dependency persistence layer that works everywhere.

### Tasks Have blocks/blockedBy Dependency Fields

Each task can declare which other tasks it blocks (downstream dependents) and which tasks block it (upstream dependencies). An agent will not start a task that has unresolved blockedBy dependencies. This is essential for multi-agent coordination: when Agent A is writing the database schema and Agent B needs to write queries against it, Agent B's task is blockedBy Agent A's task. Without dependencies, both agents might start simultaneously and Agent B would work against a schema that doesn't exist yet.

**Alternatives Considered**

Simple priority ordering (high/medium/low) doesn't capture 'task B literally cannot start until task A finishes.' A centralized coordinator that assigns tasks in order would work but creates a single point of failure and bottleneck. Declarative dependencies let each agent independently determine what it can work on by reading the task files.

### Task as Course Default, Todo Still Useful

TaskManager extends the Todo mental model and becomes the default workflow from s07 onward in this course. Both track work items with statuses, but TaskManager adds file persistence (survives crashes), dependency tracking (blocks/blockedBy), ownership fields, and multi-process coordination. Todo remains useful for short, linear, one-shot tracking where heavyweight coordination is unnecessary.

**Alternatives Considered**

Using only Todo keeps the model minimal but weak for long-running or collaborative work. Using only Task everywhere maximizes consistency but can feel heavy for tiny one-off tasks.

### Durability Needs Write Discipline

File persistence reduces context loss, but it does not remove concurrent-write risks by itself. Before writing task state, reload the JSON, validate expected status/dependency fields, and then save atomically. This prevents one agent from silently overwriting another agent's transition.

**Alternatives Considered**

Blind overwrite writes are simpler but can corrupt coordination state under parallel execution. A database with optimistic locking would enforce stronger safety, but the course keeps file-based state for zero-dependency teaching.

**s08: Background Tasks**

### threading.Queue as the Notification Bus

Background task results are delivered via a threading.Queue instead of direct callbacks. The background thread puts a notification on the queue when its work completes. The main agent loop polls the queue before each LLM call. This decoupling is important: the background thread doesn't need to know anything about the main loop's state or timing. It just drops a message on the queue and moves on. The main loop picks it up at its own pace -- never mid-API-call, never mid-tool-execution. No race conditions, no callback hell.

**Alternatives Considered**

Direct callbacks (background thread calls a function in the main thread) would deliver results faster but create thread-safety issues -- the callback might fire while the main thread is in the middle of building a request. Event-driven systems (asyncio, event emitters) work but add complexity. A queue is the simplest thread-safe communication primitive.

### Background Tasks Run as Daemon Threads

Background task threads are created with daemon=True. In Python, daemon threads are killed automatically when the main thread exits. This prevents a common problem: if the main agent completes its work and exits, but a background thread is still running (waiting on a long API call, stuck in a loop), the process would hang indefinitely. With daemon threads, exit is clean -- the main thread finishes, all daemon threads die, process exits. No zombie processes, no cleanup code needed.

**Alternatives Considered**

Non-daemon threads with explicit cleanup (join with timeout, then terminate) give more control over shutdown but require careful lifecycle management. Process-based parallelism (multiprocessing) provides stronger isolation but higher overhead. Daemon threads are the pragmatic choice: minimal code, correct behavior in the common case.

### Structured Notification Format with Type Tags

Notifications from background tasks use a structured format: `{"type": "attachment", "attachment": {status, result, ...}}` instead of plain text strings. The type tag lets the main loop handle different notification types differently: an 'attachment' might be injected into the conversation as a tool_result, while a 'status_update' might just update a progress indicator. Machine-readable notifications also enable programmatic filtering (show only errors, suppress progress updates) and UI rendering (display status as a progress bar, not raw text).

**Alternatives Considered**

Plain text notifications are simpler but lose structure. The main loop would have to parse free-form text to determine what happened, which is fragile. A class hierarchy (StatusNotification, ResultNotification, ErrorNotification) is more Pythonic but less portable -- JSON structures work the same way regardless of language or serialization format.

**s09: Agent Teams**

### Persistent Teammates vs One-Shot Subagents

In s04, subagents are ephemeral: spawn, do one task, return result, die. Their knowledge dies with them. In s09, teammates are persistent threads with identity (name, role) and config files. A teammate can complete task A, then be assigned task B, carrying forward everything it learned. Persistent teammates accumulate project knowledge, understand established patterns, and don't need to re-read the same files for every task.

**Alternatives Considered**

One-shot subagents (s04 style) are simpler and provide perfect context isolation -- no risk of one task's context polluting another. But the re-learning cost is high: every new task starts from zero. A middle ground (subagents with shared memory/knowledge base) was considered but adds complexity without the full benefit of persistent identity and state.

### Team Config Persisted to .teams/{name}/config.json

Team structure (member names, roles, agent IDs) is stored in a JSON config file, not in any agent's memory. Any agent can discover its teammates by reading the config file -- no need for a discovery service or shared memory. If an agent crashes and restarts, it reads the config to find out who else is on the team. This is consistent with the s07 philosophy: the filesystem is the coordination layer.

**Alternatives Considered**

In-memory team registries are faster but don't survive process restarts and require a central process to maintain. Service discovery (like DNS or a discovery server) is more robust at scale but overkill for a local multi-agent system. File-based config is the simplest approach that works across independent processes.

### Teammates Get Subset of Tools, Lead Gets All

The team lead receives ALL_TOOLS (including spawn, send, read_inbox, etc.) while teammates receive TEAMMATE_TOOLS (a reduced set focused on task execution). This enforces a clear separation of concerns: teammates focus on doing work (coding, testing, researching), while the lead focuses on coordination (creating tasks, assigning work, managing communication). Giving teammates coordination tools would let them create their own sub-teams or reassign tasks, undermining the lead's ability to maintain a coherent plan.

**Alternatives Considered**

Giving all agents identical tools is simpler and more egalitarian, but in practice leads to coordination chaos -- multiple agents trying to manage each other, creating conflicting task assignments. Static role-based filtering is predictable and easy to reason about.

**s10: Team Protocols**

### JSONL Inbox Files Instead of Shared Memory

Each teammate has its own inbox file (a JSONL file in the team directory). Sending a message means appending a JSON line to the recipient's inbox file. Reading messages means reading the inbox file and tracking which line was last read. JSONL is append-only by nature, which means concurrent writers don't corrupt each other's data (appends to different file positions). This works across processes without any shared memory, mutex, or IPC mechanism. It's also crash-safe: if the writer crashes mid-append, the worst case is one partial line that the reader can skip.

**Alternatives Considered**

Shared memory (Python multiprocessing.Queue) would be faster but doesn't work if agents are separate processes launched independently. A message broker (Redis, RabbitMQ) provides robust pub/sub but adds infrastructure dependencies. Unix domain sockets would work but are harder to debug (no human-readable message log). JSONL files are the simplest approach that provides persistence, cross-process communication, and debuggability.

### Exactly Five Message Types Cover All Coordination Patterns

The messaging system supports exactly five types: (1) 'message' for point-to-point communication between two agents, (2) 'broadcast' for team-wide announcements, (3) 'shutdown_request' for graceful termination, (4) 'shutdown_response' for acknowledging shutdown, (5) 'plan_approval_response' for the lead to approve or reject a teammate's plan. These five types map to the fundamental coordination patterns: direct communication, broadcast, lifecycle management, and approval workflows.

**Alternatives Considered**

A single generic message type with metadata fields would be more flexible but makes it harder to enforce protocol correctness. Many more types (10+) would provide finer-grained semantics but increase the model's decision burden. Five types is the sweet spot where every type has a clear, distinct purpose.

### Check Inbox Before Every LLM Call

Teammates check their inbox file at the top of every agent loop iteration, before calling the LLM API. This ensures maximum responsiveness to incoming messages: a shutdown request is seen within one loop iteration (typically seconds), not after the current task completes (potentially minutes). The inbox check is cheap (read a small file, check if new lines exist) compared to the LLM call (seconds of latency, thousands of tokens). This placement also means incoming messages can influence the next LLM call -- a message saying 'stop working on X, switch to Y' takes effect immediately.

**Alternatives Considered**

Checking inbox after each tool execution would be more responsive but adds overhead to every tool call, which is more frequent than LLM calls. A separate watcher thread could monitor the inbox continuously but adds threading complexity. Checking once per LLM call is the pragmatic sweet spot: responsive enough for coordination, cheap enough to not impact performance.

**s11: Autonomous Agents**

### Polling for Unclaimed Tasks Instead of Event-Driven Notification

Autonomous teammates poll the shared task board every ~1 second to find unclaimed tasks, rather than waiting for event-driven notifications. Polling is fundamentally simpler than pub/sub: there's no subscription management, no event routing, no missed-event bugs. With file-based persistence, polling is just 'read the directory listing' -- a cheap operation that works regardless of how many agents are running. The 1-second interval balances responsiveness (new tasks are discovered quickly) against filesystem overhead (not hammering the disk with reads).

**Alternatives Considered**

Event-driven notification (file watchers via inotify/fsevents, or a pub/sub channel) would reduce latency from seconds to milliseconds. But file watchers are platform-specific and unreliable across network filesystems. A message broker would work but adds infrastructure. For a system where tasks take minutes to complete, discovering new tasks in 1 second instead of 10 milliseconds makes no practical difference.

### 60-Second Idle Timeout Before Self-Termination

When an autonomous teammate has no tasks to work on and no messages in its inbox, it waits up to 60 seconds before giving up and shutting down. This prevents zombie teammates that wait forever for work that never comes -- a real problem when the lead forgets to send a shutdown request, or when all remaining tasks are blocked on external events. The 60-second window is long enough that a brief gap between task completions and new task creation won't cause premature shutdown, but short enough that unused teammates don't waste resources.

**Alternatives Considered**

No timeout (wait forever) risks zombie processes. A very short timeout (5s) causes premature exits when the lead is simply thinking or typing. A heartbeat system (lead periodically pings teammates to keep them alive) works but adds protocol complexity. The 60-second fixed timeout is a good default that balances false-positive exits against resource waste.

### Re-Inject Teammate Identity After Context Compression

When auto_compact compresses the conversation, the resulting summary loses crucial metadata: the teammate's name, which team it belongs to, and its agent_id. Without this information, the teammate can't claim tasks (tasks are owned by name), can't check its inbox (inbox files are keyed by agent_id), and can't identify itself in messages. So after every auto_compact, the system re-injects a structured identity block into the conversation: `You are [name] on team [team], your agent_id is [id], your inbox is at [path].` This is the minimum context needed for the teammate to remain functional after memory loss.

**Alternatives Considered**

Putting identity in the system prompt (which survives compression) would avoid this problem, but violates the cache-friendly static-system-prompt design from s05. Embedding identity in the summary prompt ('when summarizing, always include your name and team') is unreliable -- the LLM might omit it. Explicit post-compression injection is deterministic and guaranteed to work.

**s12: Worktree + Task Isolation**

### Shared Task Board + Isolated Execution Lanes

The task board remains shared and centralized in `.tasks/`, while file edits happen in per-task worktree directories. This separation preserves global visibility (who owns what, what is done) without forcing everyone to edit inside one mutable directory. Coordination stays simple because there is one board, and execution stays safe because each lane is isolated.

**Alternatives Considered**

A single shared workspace is simpler but causes edit collisions and mixed git state. Fully independent task stores per lane avoid collisions but lose team-level visibility and make planning harder.

### Explicit Worktree Lifecycle Index

`.worktrees/index.json` records each worktree's name, path, branch, task_id, and status. This makes lifecycle state inspectable and recoverable even after context compression or process restarts. The index also provides a deterministic source for list/status/remove operations.

**Alternatives Considered**

Relying only on `git worktree list` removes local bookkeeping but loses task binding metadata and custom lifecycle states. Keeping all state only in memory is simpler in code but breaks recoverability.

### Lane-Scoped CWD Routing + Re-entry Guard

Commands are routed to a worktree's directory via `worktree_run(name, command)` using the `cwd` parameter. A re-entry guard prevents accidentally running inside an already-active worktree context, keeping lifecycle ownership unambiguous.

**Alternatives Considered**

Global cwd mutation is easy to implement but can leak context across parallel work. Allowing silent re-entry makes lifecycle ownership ambiguous and complicates teardown behavior.

### Append-Only Lifecycle Event Stream

Lifecycle events are appended to `.worktrees/events.jsonl` (`worktree.create.*`, `worktree.remove.*`, `task.completed`). This turns hidden transitions into queryable records and makes failures explicit (`*.failed`) instead of silent.

**Alternatives Considered**

Relying only on console logs is lighter but fragile during long sessions and hard to audit. A full event bus infrastructure is powerful but heavier than needed for this teaching baseline.

### Close Task and Workspace Together

`worktree_remove(..., complete_task=true)` allows a single closeout step: remove the isolated directory and mark the bound task completed. Closeout remains an explicit tool-driven transition (`worktree_keep` / `worktree_remove`) rather than hidden automatic cleanup. This reduces dangling state where a task says done but its temporary lane remains active (or the reverse).

**Alternatives Considered**

Keeping closeout fully manual gives flexibility but increases operational drift. Fully automatic removal on every completion risks deleting a workspace before final review.

### Event Stream Is Observability Side-Channel

Lifecycle events improve auditability, but the source of truth remains task/worktree state files. Events should be read as transition traces, not as a replacement state machine.

**Alternatives Considered**

Using logs alone hides structured transitions; using events as the only state source risks drift when replay/repair semantics are undefined.