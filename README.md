# miniCode

This project is an implementation of https://learn.shareai.run/.

**Count Lines of Code**

```
-------------------------------------------------------------------------------
Language                     files          blank        comment           code
-------------------------------------------------------------------------------
Python                           1             13              6             77
Markdown                         1             12              0             30
TOML                             1              0              0             10
-------------------------------------------------------------------------------
SUM:                             3             25              6            117
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

```python
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