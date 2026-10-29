# Local Agent: a Claude-style AI helper that runs fully on your own PC

A chat app where a local AI model can actually do things on your computer: read and edit files,
run commands, programs and tests and fix what fails, search the web, plan multi-step work and
remember things between chats. It only touches your mouse, keyboard or screen while you have
**Computer use** turned on. Nothing goes to the cloud: the model runs on your GPU.

It also starts the model for you, with the speed settings worked out for an
**RTX 5080 (16 GB) + 64 GB RAM**.

## Install (once)

1. Install **Python 3.12** from [python.org](https://www.python.org/downloads/). Tick
   *"Add python.exe to PATH"* during install.
2. Make sure your **NVIDIA driver is up to date** (GeForce Experience / NVIDIA App). The newest
   llama.cpp builds need a recent driver.
3. Double-click **`setup.bat`** in this folder and wait for "Setup done".
4. Double-click **`run.bat`**.

## First run

The chat page shows **No model running yet. Pick one to get started** with the two ready-made
modes (see [Models](#models-for-a-5080-16-gb--64-gb-ram)). Click one: the app downloads what's
missing (the model, its vision file and llama.cpp; downloads resume if they're interrupted) and
starts it. When the dot next to the model's name (bottom right of the message box) turns green, ask
for something. Next time it starts the same
model by itself when the app opens (turn that off under Model server > Advanced).

You can switch modes any time from the model menu at the bottom right of the message box.

## The window

It's laid out like the Claude app:

- **On the left**: **New chat** (Ctrl+N), the app's pages, then your past chats by date (Today,
  Yesterday, Previous 7 days...). Click one to carry on in it; right-click to rename or delete it;
  type in **Search chats** to find one. **«** or Ctrl+B hides the list, **☰** brings it back.
- **The message box** at the bottom: **+** adds an image or a screenshot or changes the work folder,
  then what it may do without asking, how long it thinks, **Computer use** (on/off, its arrow picks
  the screen) and the model. **↑** sends (or Enter), and
  turns into **■** Stop while it works (or Esc).
- **Text size**: Ctrl + bigger, Ctrl - smaller, Ctrl 0 back to normal, or Ctrl + mouse wheel over the
  chat. Settings > Look has the same, and switches answers between sans serif
  (the default) and serif.
- **Plan & context** (top right) opens the plan for bigger tasks, how full the context is and the
  work folder. It opens by itself when a plan comes in.
- Each answer has **Copy** under it.

Using a model file of your own instead:

1. Go to the **Model server** page and pick **Another model file** under Mode.
2. Press **Models folder...** and pick the folder with your `.gguf` models (LM Studio's folder is
   found by itself). Choose a model in the list. The page shows what's inside it and whether it
   fits on your card.
3. Want it to see images (pictures you paste, Unity's game view)? If the page says there's no
   vision file, press **Find vision file online**. It works out which base model yours is built
   on (for example `Swift-1.5-Qwen3.8-27B-...` is built on Qwen3.8-27B), finds that model's vision
   file (`mmproj`) on Hugging Face, checks it fits, and downloads it next to your model. It's
   usually under 1 GB.
4. Press **Start model** (it offers to download llama.cpp first if it isn't on the PC yet).

Already using **LM Studio** or **Ollama**? Skip the Model server page: in **Settings > Connection**
pick it from the list, and load the model there with tool calling on. (Ollama's model files
aren't `.gguf` files you can browse to, so for Ollama models use Ollama itself.)

## What it can do

| Group | Tools |
|---|---|
| Files | read (with line numbers, also shows images), write, exact edit, list, find by name, search inside files, move, copy, delete (to the Recycle Bin) |
| Commands | run PowerShell commands and programs, run long ones (servers, games, watchers) in the background and read their output, run Python scripts |
| Web | search (DuckDuckGo, no account), read pages |
| Git and GitHub | clone, status, diff, log, branch, commit, pull, push; list your repos; list, read and open pull requests (and ask for reviewers); list and open issues; comment |
| Coding | work in a private git copy of a repo, run the tests until they pass, map a project's classes and functions, several exact edits at once, a syntax check after every edit |
| Memory | save notes, search notes and your indexed files, forget notes, add a folder to the index |
| Thinking | a to-do plan shown on the right, ask you a question, wait, hand a big search to a helper agent with its own fresh memory |

Things it does the way Claude does:

- **Works in a loop**: it calls tools, reads the results, and keeps going until the job is done
  (no step limit: one message runs until the job is done or you press Stop).
- **Checks its own work by running it**: it runs the program, script or tests, reads what they
  print, fixes what fails and runs them again until it works. See [Computer use](#computer-use).
- **Plans** bigger jobs with a visible to-do list.
- **Keeps long tasks going**: when the conversation gets near the model's memory limit, older
  parts are summarized automatically, and old images are dropped.
- **Recovers from model mistakes**: broken tool-call JSON is repaired or sent back to fix, tool
  calls written as text are picked up, it's told when it repeats the same action, and servers
  without proper tool support get the tools described in the prompt instead.
- **Reads `AGENT.md`**: put standing instructions in a file called `AGENT.md` in the work folder.
- Paste images into the chat (Ctrl+V), attach files, or press **Add screenshot** to show it your
  screen yourself.

## Coding mode

Ask it to change code in a git repository ("fix the crash when the config file is missing in
my body-tracker repo") and it works the way Claude Code does:

1. It makes a **private copy** of the repo on a new branch (a git worktree, in
   `.worktrees` inside the work folder). Your own checkout and files aren't touched.
2. It maps the code (classes and functions per file) to find its way around.
3. It edits, and every edit gets a quick syntax check so a broken file is noticed at once.
4. It **runs the tests** (pytest, npm, cargo, go, dotnet, maven or gradle, found
   automatically), reads the failures, fixes them, and runs them again until they pass. It
   isn't allowed to delete or weaken tests to get there. If it's stuck after six failed runs
   in a row, it stops and tells you what fails.
5. It reviews its own diff, commits, and if you asked, pushes and opens a pull request.

Packages your project needs (node_modules, .venv) aren't copied into the private copy. For
Python it uses your project's `.venv` automatically; for other languages it installs them in
the copy when the tests need them. Turn coding mode off in Settings if you don't want it.

## Long-term memory

The AI keeps short notes between chats: your preferences, where your projects are, what fixed
a problem. To keep it fast and focused it is **not** shown all of them. With each message it
sees only the few notes that clearly match what you asked (plus any you pin), and it can
search the rest whenever it needs to. After bigger tasks it asks itself what's worth
remembering and saves up to three notes, updating old ones instead of piling up duplicates.

It also keeps a **search index of your files** (the work folder, plus any folders you add on
the Memory page), so it can find "the budget spreadsheet" or "where I handle logins" by what's
inside them. The index updates itself every 10 minutes and only re-reads changed files.

Everything is in `data/memory.db` on your PC. The **Memory** page lets you read, search,
edit, pin and delete notes, and pick which folders are indexed. Your old `memory.md` notes
are moved in automatically.

## Unity

It can work inside the **Unity Editor** while you have your project open: see the scene, place
and change objects, set component values, make prefabs and materials, write scripts and fix
their compile errors, play the game for a few seconds to test, and read the console.

1. Open your project in Unity.
2. Ask, for example: "set up Unity for my project in D:/Games/MyGame". It adds one small editor
   script (`Assets/Editor/LocalAgent/`) to the project. Click into the Unity window once so
   Unity loads it. (Or copy the two files from this app's `unity` folder there yourself.)
3. Ask for things: "add a red cube that falls when the game starts", "why does the player fall
   through the floor?", "make an enemy prefab that walks toward the player".

How it works on a script change: it writes the `.cs` file, makes Unity compile it, reads the
errors, fixes them until it compiles, adds the script to an object, then plays the game for a
few seconds and checks the console and a picture of the game view.

Good to know:
- Its scene changes go through Unity's undo, so **Ctrl+Z in Unity** takes them back. It doesn't
  save the scene unless you ask (or it finishes a task you gave it).
- Changes made during play mode are lost when play stops, like in Unity itself, so it stops
  play mode before editing.
- The editor script only listens on your own PC (127.0.0.1) and needs a random key that Unity
  writes into the project's `Library` folder. Delete `Assets/Editor/LocalAgent` to remove it.
- If Unity is minimized and answers slowly, set Unity's *Preferences > General > Interaction
  Mode* to *No Throttling*.
- Pictures of the game view come from the cameras, so screen-space overlay UI doesn't show.
- Turn the Unity tools off in Settings if you don't use Unity; they take a little room in
  the model's context.

## GitHub

It can work on your GitHub repositories the way Claude does here: clone one, make a branch,
change code, commit, push, and open a pull request (optionally asking someone to review).

1. Install **Git for Windows** from [git-scm.com](https://git-scm.com/download/win) if you don't
   have it.
2. Sign in, one of two ways:
   - **Token:** Settings > GitHub > **Make a token...** opens GitHub. Make a fine-grained token,
     pick the repositories it may use, and give it *Contents: read and write*, *Pull requests: read
     and write* and *Issues: read and write*. Paste it into the Token box and press **Check sign-in**.
   - **GitHub CLI:** if you have `gh`, run `gh auth login` once in a terminal. The app uses that login.
3. Ask, for example: "clone aidandillon3/random-project, fix the typo in the README, and open a
   pull request".

The token is saved only in `data/settings.json` on your PC (that folder is never committed). It's
sent only to GitHub, and the git and GitHub tools blank it out of anything they show the model.

## Add-on tools: MCP servers and your own plugins

**MCP servers.** Any [Model Context Protocol](https://modelcontextprotocol.io) server (the same
ones Claude Desktop, Cursor and LM Studio use) adds its tools: a web browser, databases, Blender,
Home Assistant, Notion and hundreds more. Settings > Add-on tools > **Edit MCP servers...** opens
the list, in Claude Desktop's format:

```json
{"mcpServers": {
  "browser": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
  "fetch":   {"command": "uvx", "args": ["mcp-server-fetch"], "risk": "safe"}
}}
```

Each server is a program on your PC (`npx` needs [Node.js](https://nodejs.org), `uvx` needs
[uv](https://docs.astral.sh/uv/)). Their tools are named `server__tool` and ask before running,
like commands do. `"risk": "safe"` lets a server you trust to only read run without asking;
`"hints"` trusts the read-only marks the server puts on its own tools. `"disabled": true` turns one
off. Servers start when the app opens; their tools are in every chat started after. Every tool
costs context, so keep the list to servers you use.

**Plugins.** A Python file in the plugins folder (Settings > Add-on tools > **Open plugins
folder**) can add tools with the same decorator the built-in tools use:

```python
from localagent.tools.base import tool

@tool("word_count", "Count the words in a text file.",
      {"path": {"type": "string", "description": "The file, relative to the work folder"}}, required=["path"])
def word_count(ctx, a):
    return f"{len(ctx.resolve(a['path']).read_text(encoding='utf-8').split())} words"
```

Give it `risk=EDIT`, `RUN` or `PUBLISH` (from the same module) when it changes something, so it
asks first. Press **Reload** after changing a plugin; a plugin can't replace a built-in tool.

## Safety: what it asks before doing

In the message box, the first picker (**Ask first** by default):

| Mode | Asks before |
|---|---|
| Read only | Never changes anything (refuses edits and commands) |
| **Ask before risky actions** (default) | Every file change or command |
| Auto-approve file edits in the work folder | Commands, and edits outside the work folder |
| Full auto | Only clearly dangerous commands (formatting, recursive deletes, registry deletes, shutdown...) |

Pushing code and posting to GitHub (pull requests, issues, comments) count as a separate kind of
action that **asks even in Full auto**, because it goes out under your name. You can turn that off
in Settings > GitHub. It never force-pushes.

In the approval box you can also pick **Allow all ... for this chat**, or **Deny** and type what
it should do instead. Deleted files go to the Recycle Bin.

**Emergency stop:** press **Stop** (or Esc).

## Computer use

Off by default. Then it works the way Claude Code does: it tests and debugs by running things and
reading what they print (test results, errors, logs), never by clicking. The programs it runs
still run on your PC, so one with a window shows that window, but it can't click in it. For Unity
it uses the editor's own tools (play mode, the console, pictures of the game view) instead.

Press **Computer use** next to the message box to let it see and use your screen, like Claude's
computer use: it takes a screenshot, moves the mouse, clicks, scrolls, drags, types and presses
shortcuts, lists and switches windows, and reads buttons by name in Windows apps. Press it again to
turn it off; that takes effect from its next click. While it's on:

- **It doesn't ask** before a click or keypress. Opening a program file (.exe, .bat...) still
  follows the approval mode, like any command. Read only mode still blocks everything.
- **Emergency stop**: slam the mouse into a corner of the screen, or press Stop.
- **Which screen**: the arrow on the button (or Settings > Computer use) picks the screen it looks
  at first; it can look at the others too. This window minimizes while it uses the screen it's on,
  and comes back when it's done.
- **Clicking by words**: `click_text` reads the screen with Windows' own text recognition and
  clicks the words it's told ("SELL ALL", "File"), also in games, where Windows accessibility sees
  nothing. `screenshot` with marks numbers the buttons and short text on the picture, so it can
  click "mark 7" instead of aiming.
- **Its own browser**: for websites it drives Edge (or Chrome) directly, in a profile of its own in
  `data/browser-profile` (your own browser and its logins are left alone). Pages come back as
  text with numbered links, buttons and fields, so it needs no screenshots there.
- Settings > Computer use also has the click coordinates (automatic: a 0-1000 grid for Qwen
  models), the screenshot size (automatic: about 960x512 on a 16:9 screen, which read 40% quicker
  than 1344x768 in the test and did as well; zooms are always sharp), a screenshot after every
  action, a coordinate grid for weaker models, marks on every screenshot, and switches for text
  clicking and the browser.

The first time you turn it on it offers to install what it needs (mss, pyautogui, pyperclip,
pywinauto; about 20 MB) and the text reading and browser add-ons (winrt, playwright; about 60 MB).
Running `setup.bat` again does the same.

### The computer use test

Settings > Computer use > **Run the computer use test** (or `computer-use-test.bat`) has the
model do 8 small tasks with your real mouse and keyboard, in a window that fills its screen:
click a button by its words, click a tiny dot, type into a box, press a button in a painted
game-style menu, set a slider, drag a box, hold a key, and fill in a web form. Each task checks
itself and has 150 seconds. It can also run everything again with the new helpers off, with
thinking on Low, or with bigger screenshots, to compare. The summary shows what worked and how long the model and the tools
took; every step (thinking, calls, timings, tokens) is saved in `data/bench/` to find what's slow.
Don't touch the mouse while it runs; push it into a screen corner to stop.

Used an older version with its own virtual computer? Delete the `data/vdesktop` folder (the
Ubuntu download, about 390 MB). If you got as far as setting it up, first run
`wsl --unregister LocalAgentDesktop` in a terminal.

## Models for a 5080 (16 GB) + 64 GB RAM

**Ready-made modes** (Model server page > Mode): pick the model, then its size (quant) in the list
beside it, and press the button. The message box's model menu has the same, one sub-menu per model. If the files
aren't on your PC it downloads them first (it can pause and resume), then switches the model,
its vision file and the server settings, and restarts the model. Sampling and thinking levels
follow whichever model is running.

| Mode | Download | How it runs | Pick it for |
|---|---|---|---|
| **Swift 1.5 Qwen3.8 27B** (IQ3_XXS, MTP) | 11 GB | Fully on the GPU, MTP speed-up, 48K context | Everyday work, fastest |
| **Swift 1.5 Qwen3.8 27B** (IQ3_S, MTP) | 12.8 GB | Same, about half the drift from the full model, ~15% slower; needs a GPU with no monitors on it | Everyday work on a free 16 GB card |
| **Swift 1.5 Qwen3.8 Flash Next IQ2_XS** | 69 GB (+6 GB for Strata) | 125B MoE, 6B active, on Strata: busiest experts on the GPU, the rest in RAM, its 28 GB n-gram table read from the SSD as needed, 64K context | Hardest tasks |
| **Swift 1.5 Qwen3.8 Flash Next IQ3_XXS** | 77 GB | The same model at 3 bits: 43 GB of experts in RAM, about 20% slower than IQ2_XS | Hardest tasks, better answers |
| **Swift 1.5 Qwen3.8 Flash Next IQ3_S** | 85 GB | The same model at 3.5 bits: 50 GB of experts in RAM, about a third slower than IQ2_XS | Best answers, with little else open |
| **Qwen3.8 Flash Next Coder IQ1_M** | 59 GB | The original Flash Next with half its experts removed, the rest stored like IQ3_S: 23 GB of experts in RAM | Coding, with Unity or a game open |
| **Qwen3.6 35B-A3B UD-Q4_K_XL** (Strata fork) | 23 GB | Qwen's smaller MoE (3B active) on a Strata fork tuned for it, most experts on the GPU, 262K context, no vision | Fast tool and test loops; about twice Flash Next's speed, weaker on hard tasks |
| **Ornith 1.5 122B** (UD-IQ3_XXS, UD-Q3_K_XL, UD-Q2_K_XL, Q3_K_L + MTP) | 45-66 GB | A coding and agent fine-tune of a 122B MoE, on llama.cpp with its experts in RAM | Trying the newest coding model; slower than Flash Next on Strata |
| **Qwen3.8 27B 1-bit + NVFP4 prefill** (experimental) | 20 GB | A 1-bit 27B on the GPU writes; a 4-bit copy of its layers, streamed from the SSD, reads your prompt. No vision, no MTP | Trying the 1-bit research trick |

Flash Next IQ2_XS needs about 40 GB of RAM + VRAM plus space on a fast SSD. It runs on
**[Strata](https://github.com/Niko1221/Strata)** by default (the "Run it with Strata" box under
Mode), an engine made for this one model. Strata keeps the most-used experts on the GPU, works the
rest out on the CPU at the same time, and adds a draft layer that guesses a few tokens ahead.
Strata measured 79 tokens/s for IQ2_XS on a 12 GB RTX 5070, where llama.cpp manages roughly
20-27; nobody has measured a 5080 yet. The first start downloads Strata (pinned to a checked
version, v0.1.45 since 2026-10-20; an older install is updated and set up again on the next start) into
`strata/` next to the app and runs its setup, which installs Python if needed and
fetches its engine and the draft layer into `Strata-data` beside your models folder. Your model
files are used where they are. Untick the box to run Flash Next on llama.cpp instead. Strata can't
run the 27B, which stays on llama.cpp.

The app also turns on Strata's two opt-in speed-ups for one card whose RAM holds every expert (Strata's
README): `--adapt-async 1` updates the expert cache on a helper thread instead of pausing the answer
(+4-5% writing, +7-10% on code and long documents on an RTX 5070; answers can differ slightly from
Strata's default but stay the same from run to run), and `STRATA_PIN_BY_NEED=1` locks the experts the
card needs most first (+4% prompt reading on Windows). To turn one off, set it to `0` in
`strata-<size>.json` (in `strata/`): the app keeps values you set.

The two 3-bit sizes answer closer to the full model (IQ3_S matches it on the published tests) but
write slower: 62 tokens/s for IQ3_XXS and 53 for IQ3_S on the same RTX 5070. Strata keeps every
expert in RAM and wants about 10 GB beside them for Windows and your other programs, so on a 64 GB
PC IQ3_S leaves little room: close big programs (Unity, games) while it runs, or take IQ3_XXS. All
three sizes share the vision file and Strata's draft layer; each size runs Strata's setup once, the
first time it starts. Once you've settled on a size, delete the other sizes' two `.gguf` files from
`Swift-1.5-Qwen3.8-Flash-Next` to get the space back.

The Coder mode is [ISTA-DASLab's coding version](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF)
of Flash Next: it keeps the 256 of each layer's 512 experts that code, tool use and images use and
drops the rest. Its makers measured 99% of the full model's LiveCodeBench score and 91% of its
SWE-bench Verified score (fixing real bugs in real repos, the closest to this app's coding mode).
It is weaker at everything else and in languages other than English. Strata measured 55 tokens/s
on the RTX 5070, about IQ3_S's speed, and it reads long prompts the fastest of all the sizes. Its
experts take only 23 GB of RAM, so Unity or a game can stay open. It is the plain Qwen model, not
the Swift fine-tune, so it thinks for longer before it answers. It has its own folder
(`Qwen3.8-Flash-Next-Coder`) and vision file, and Strata sets it up once the first time it starts.
Switch to it for coding from the model menu in the message box, and back to a Swift size for the rest.

The Qwen3.6 35B mode runs [Qwen3.6-35B-A3B](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-MTP-GGUF)
(Unsloth's UD-Q4_K_XL with its MTP layer) on [xyehya's Strata fork](https://github.com/xyehya/Strata/tree/q36-r2),
branch `q36-r2`, with the settings and expert profile from
[ykarout's model card](https://huggingface.co/ykarout/Strata-Qwen3.6-35B-A3B). By that card, on an RTX 5080 with
64 GB of RAM it writes about 230 tokens/s at 4K context and 160 at 240K, peaks near 290 in agent work, and reads
prompts at about 6,500 tokens/s (on the Sage fine-tune of the same model, the released Strata engine wrote about
173 and llama.cpp about 166, reading at 3,200). Those are one
person's runs, and nobody has measured it with this app yet. It is a much smaller model than Flash Next, so
expect weaker answers on hard tasks (ykarout's one agent test: 62 of 83 hidden tests passed). The fork has no
Windows engine, so this repo's `Strata Qwen3.6 engine for Windows` job (under Actions) builds one for RTX 50 cards
and puts it in a pre-release. The first start downloads the fork into `strata-q36` beside the app's `strata`
folder (Flash Next keeps its own pinned Strata), downloads that engine (it needs the GitHub token in Settings >
GitHub, as the 1-bit mode does), and runs the fork's setup. Vision is off in this mode: the engine build has no
image reader.

The 1-bit mode is IST-DASLab's [disaggregated prefill](https://huggingface.co/ISTA-DASLab/Qwen3.8-27B-NVFP4-prefiller)
research. A 1-bit model is small but answers badly, mostly because it misreads the prompt. Here a
4-bit (NVFP4) copy of the 27B's layers, trained to go with the 1-bit one, reads the prompt instead:
it streams off the SSD one layer at a time, so it takes no VRAM, and the 1-bit model only writes.
By the paper this wins back 32.5 points on MMLU-Pro at 1 bit (about 7 at 2 bits, nothing at 3), so
it still answers below the 3-bit 27B mode; it's for trying the idea, not for daily use. Every
prompt streams all 12.8 GB of blocks once, so the first word takes a few seconds even for a short
message (a fast NVMe SSD helps), and the MTP speed-up and vision are off.

It needs IST-DASLab's own llama.cpp, which has no Windows build, so this repo's GitHub Actions
builds one (the "ODP llama.cpp for Windows" job, which patches its file reading for Windows) and
publishes it as a pre-release. The repo is private, so the first Start asks to download it with
the GitHub token from Settings > GitHub (it needs read access to the repo's contents) into
`odp-llama/` next to the app. The build uses CUDA 13, which needs NVIDIA driver 580 or newer. The
fork is a research prototype tested by its makers on Linux only.

| Model | Type | Sees images | On your PC | Pick it for |
|---|---|---|---|---|
| **Qwen3.6-35B-A3B** (Q4_K_M) | MoE, 3B active | Yes | ~21 GB: attention on the GPU, some experts in RAM. Fast | **Best all-rounder for this app** |
| **Qwen3.8-27B** (IQ4_XS or Q3_K_M) | Dense | Check the model page | Q4_K_M (~18 GB) spills into RAM and slows down; IQ4_XS/Q3 fit | Smartest model that runs locally, slower |
| **Qwen3.6-27B** (IQ4_XS, "MTP" upload) | Dense | Yes | ~14 GB, fits | Hard coding/reasoning; MTP speed-up about 1.3-2x |
| **gpt-oss-20b** (MXFP4) | MoE, 3.6B active | No | ~13 GB, fully on the GPU. Very fast | Fast text and coding work |

Search Hugging Face for the name plus "GGUF" (bartowski and unsloth make good ones), and for
vision download the matching **mmproj** file into the same folder as the model.

Ornith 1.5 122B ([ornith-ai](https://huggingface.co/ornith-ai/Ornith-1.5-122B-GGUF), MIT licence, out
on 2026-10-20) is a coding and agent fine-tune of a 122B Qwen3.5-style MoE. Its makers' table puts it
level with Flash Next: ahead on SWE-bench Pro, NL2Repo, HLE and the search tests, behind on
TerminalBench 2.1 and Toolathlon. Its SWE-bench Pro lead (76.9 against 43.9) uses a score for Flash
Next well below Qwen's own (62.5), so take that one with salt. Strata runs only Flash Next, so Ornith
runs on llama.cpp with its experts in RAM, which is much slower than Flash Next on Strata (nobody has
measured a 16 GB card yet). Four sizes: UD-IQ3_XXS (50 GB, the pick), UD-Q3_K_XL (61 GB, better but
leaves little RAM), UD-Q2_K_XL (45 GB) and Q3_K_L with its MTP layers kept (66 GB, from
prithivMLmods: the MTP speed-up guesses words ahead, but it is the most a 64 GB PC can take). The
Unsloth sizes use the n-gram speed-up instead. All four see images and use the model card's coding
sampling (temperature 0.6).

What's checked and what's guessed: Qwen3.6-35B-A3B, Qwen3.6-27B and Qwen3.8-27B were checked
against model pages in October 2026, and the 27B file sizes come from a real quant table. Speed
numbers in the app are estimates from memory bandwidth, not measurements on your PC. Whether
Qwen3.8-27B includes vision wasn't confirmed. Any model with tool calling works here.

## The speed tricks (all set up for you)

| Trick | What it does | In this app |
|---|---|---|
| Flash attention | Faster attention, less memory | Always on |
| KV cache q8_0 | Context uses half the memory, practically no quality loss | Default; q4_0 option |
| MoE expert offload + auto-fit | Rarely-used experts go to RAM, everything else stays on the GPU, so a 35B MoE runs fast on 16 GB | Automatic (llama.cpp `--fit`, or the app's own plan on older builds) |
| MTP speculative decoding | Models with built-in prediction layers write about 1.3-2x faster | Speed-up option (works with vision) |
| Draft model | A tiny model of the same family guesses ahead | Speed-up option |
| N-gram speculation | Reuses text already in the chat; fast when editing code | Speed-up option |
| Prompt caching | The server remembers the conversation so each step only reads what's new; the app avoids rewriting old messages so the cache keeps working | Always on |
| Quant choice | Q4_K_M is the sweet spot; IQ4_XS ~10% smaller; below Q3 gets dumb | Shown per model |
| GPU choice | With two or more NVIDIA cards, the model runs on the one you pick (or all of them); the others stay free for your monitors and games | **Run the AI on** on the Model server page (newest card by default) |
| Desktop VRAM | With your monitors on the AI's GPU, Strata and Basalt leave 1.7 GB free for them (else Windows pages GPU memory and speed drops); with the monitors on another card, the model gets that VRAM | **My monitors are plugged into the AI's GPU** on the Model server page (found by itself until you change it) |

Smarter answers: set **Thinking** in the message box to High for hard tasks, Off or Low for quick
jobs. Changing it mid-chat makes the next reply re-read the chat once, so pick it before you
start. Sampling follows the model maker's recommended values by itself (Settings > Advanced).

## Other ways to run it

- **`run-terminal.bat`**: the same agent in a terminal window.
- **`get-llama-cpp.bat`**: download llama.cpp without opening the app.
- Your settings, chats and memory live in the `data` folder here; delete it to start fresh.

## If something's wrong

| Problem | Fix |
|---|---|
| "Can't reach the model server" | Start the model on the Model server page, or check the address in Settings |
| The server stops right after starting | Read the log under the Start button. "out of memory": pick a shorter context or q4_0 KV cache. "CUDA driver version is insufficient": update the NVIDIA driver |
| It writes tool calls as plain text, or ignores tools | Use a model made for tool calling (the list above). The app already starts llama.cpp with `--jinja`, which most models need |
| Unity tools say no editor is running | Open the project in Unity, then ask it to set up Unity for that project. If the scripts have compile errors, choose *Ignore* when Unity offers Safe Mode |
| It can't see images | Press **Find vision file online** on the Model server page, and keep "Let it see images" on |

## For the curious

- `localagent/agent.py`: the loop, approvals, context summarizing, memory recall.
- `localagent/memory.py`: notes and the file index (SQLite full-text search).
- `localagent/tools/code.py`: coding mode (private copies, test loop, code map).
- `localagent/tools/unity.py` + `unity/LocalAgentBridge.cs`: the Unity tools and the editor script they talk to.
- `localagent/tools/`: every tool; adding one is a single decorated function.
- `localagent/mcp.py` + `plugins.py`: MCP servers and plugin files as extra tools.
- `localagent/launcher.py` + `gguf.py`: reads the model file header and plans GPU/RAM placement.
- `tests/`: run `python -m pytest tests` (uses a fake model server; no GPU needed).
