# miniCode

This project is an implementation of https://learn.shareai.run/.

**Count Lines of Code**

```
-------------------------------------------------------------------------------
Language                     files          blank        comment           code
-------------------------------------------------------------------------------
Python                           1             24              6            135
Markdown                         1             20              0             60
TOML                             1              0              0             10
-------------------------------------------------------------------------------
SUM:                             3             44              6            205
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