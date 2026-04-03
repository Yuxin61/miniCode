# miniCode

This project is an implementation of https://learn.shareai.run/.

**Count Lines of Code**

```
-------------------------------------------------------------------------------
Language                     files          blank        comment           code
-------------------------------------------------------------------------------
Python                           1             63             52            508
Markdown                         1             76              0            259
TOML                             1              0              0             10
-------------------------------------------------------------------------------
SUM:                             3            139             52            777
-------------------------------------------------------------------------------
```

## dotenv

```properties
ANTHROPIC_BASE_URL=
ANTHROPIC_AUTH_TOKEN=
MODEL_ID=
```

## Stages

### s01. The Agent Loop

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

- Bash

### s02: Tool Use

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

1. Bash
2. \*Read File
3. \*Write File
4. \*Edit File

### s03: Todo List

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

1. Bash
2. Read File
3. Write File
4. Edit File
5. \*Todo

### s04: Subagent

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

1. Bash
2. Read File
3. Write File
4. Edit File
5. Todo
6. \*Task (subagent, only parent)

### s05: Skills

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

1. Bash
2. Read File
3. Write File
4. Edit File
5. Todo
6. Task (subagent, only parent)
7. \*Skill

### s06: Compact

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

### s07: Task Manager

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

### s08: Background Tasks

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

## Explorer

TODO.