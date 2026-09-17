#!/usr/bin/env python3

"""Review Dependabot pull requests with AI-assisted analysis."""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path
from typing import List, Literal

import yaml
from pydantic import BaseModel, Field

REPO = "confidential-containers/cloud-api-adaptor"

class CodeRecommendation(BaseModel):
    """A concrete code fix required to adapt the project to a breaking change."""
    file: str = Field(description="Repo-relative path of the file to change")
    description: str = Field(description="Short imperative summary of the change")
    old_code: str = Field(description="Exact existing code to replace")
    new_code: str = Field(description="Replacement code")


class BreakingChange(BaseModel):
    """A breaking/backwards-incompatible change identified from a changelog."""
    dependency: str = Field(description="Dependency that introduced the change")
    api: str = Field(
        description="The specific affected API, e.g. 'InstanceProfile.VcpuCount "
                    "response shape' or 'func NewClient signature'")
    description: str = Field(description="What changed and why it is breaking")
    symbols: List[str] = Field(
        default_factory=list,
        description="Concrete Go identifiers to grep for in the codebase to "
                    "check impact: type, function, method, field, or constant "
                    "names, e.g. ['SupportedVcpuCount', 'VcpuCount']")


class DependencyAssessment(BaseModel):
    """Per-dependency analysis."""
    name: str = Field(description="Dependency name")
    change: str = Field(description="Version change, e.g. '1.21.1 -> 1.22.0 (minor)'")
    risk: Literal["LOW", "MODERATE", "HIGH"] = Field(
        description="Risk level for this dependency")
    notes: str = Field(
        description="A few sentences: what changed per the changelog, any breaking "
                    "or deprecated APIs, and whether they affect this codebase")


class Assessment(BaseModel):
    """Structured result of an AI Dependabot review."""
    verdict: Literal["APPROVE", "DO_NOT_APPROVE"] = Field(
        description="Whether the PR is safe to merge")
    risk_level: Literal["LOW", "MODERATE", "HIGH"] = Field(
        description="Overall risk of the update")
    summary: str = Field(
        description="One or two sentence summary of the assessment")
    changelog_summary: str = Field(
        description="Summary of the upstream changelog/release notes, or a note "
                    "that none were available")
    dependencies: List[DependencyAssessment] = Field(
        default_factory=list,
        description="Per-dependency analysis, one entry for each updated dependency")
    breaking_changes: List[BreakingChange] = Field(
        default_factory=list,
        description="Breaking/backwards-incompatible changes found in the "
                    "changelogs; empty if none")
    impact: str = Field(
        default="",
        description="Impact analysis of the breaking changes on this project's "
                    "sources: whether the affected APIs are actually used and "
                    "where; empty if there are no breaking changes")
    reasoning: str = Field(
        description="Detailed, multi-paragraph justification for the verdict: "
                    "breaking-change analysis, whether the risk is acceptable for "
                    "the semver classification, and whether the scope is contained")
    recommendations: List[CodeRecommendation] = Field(
        default_factory=list,
        description="Concrete code fixes required to adapt this project to a "
                    "breaking change, empty if none")


SYSTEM_PROMPT = textwrap.dedent("""\
    You are a senior software engineer reviewing a Dependabot pull request for the
    cloud-api-adaptor project (Go + GitHub Actions). Assess the update and return a
    structured assessment.

    You will be given:
    - The list of dependencies being updated with old/new versions and semver classification
    - Changelog/release-notes excerpts from upstream
    - The PR diff context

    Produce a thorough review, not a superficial one. Specifically:
    - 'changelog_summary': summarize the upstream changelog/release notes; if none
      were provided, say so and reason from the semver classification.
    - 'dependencies': add one entry per updated dependency with its version change,
      risk, and notes on what changed and whether it affects this codebase.
    - 'breaking_changes': list every breaking or backwards-incompatible change you
      find in the changelogs. For each, name the specific affected API and, in
      'symbols', list the concrete Go identifiers (type, function, method, field,
      or constant names) a reviewer should grep for to check whether this project
      uses it. Leave empty only if there are genuinely no breaking changes.
    - 'reasoning': a detailed, multi-paragraph justification covering whether any
      breaking changes affect this codebase, whether the risk is acceptable for the
      semver classification, and whether the update scope is well-contained.
    - 'verdict': APPROVE or DO_NOT_APPROVE, with 'risk_level' and a short 'summary'.

    Leave 'recommendations' empty in this first pass; a later pass proposes fixes
    when breaking changes actually impact the project.
""")

IMPACT_AGENT_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a senior Go engineer reviewing a Dependabot pull request for the
    cloud-api-adaptor project. A first-pass review already identified one or more
    breaking changes in the updated dependencies. Your only job is to determine
    whether THIS project's own source code is actually impacted by them.

    You can call read_file to read any source file in the project. A list of files
    that reference the changed dependency is provided as a starting point, but you
    are free to read any other file you need — follow imports, wrappers, and
    interface definitions until you are confident. Read the actual code; do not
    guess about usages.

    Impact is NOT only a compile-time question. A breaking change impacts this
    project if it makes the code fail to BUILD *or* changes its RUNTIME behavior
    for any input the project could encounter. Trace what the changed API can now
    return or do, and weigh it against every assumption the surrounding code
    makes about it. Reason about the full range of inputs and configurations the
    project could face in production, not just the ones its tests or CI exercise:
    code compiling and existing tests passing does NOT prove the project is safe.

    When you have enough information, call submit_assessment. Decide the verdict
    on IMPACT ALONE:
    - 'impact': for each breaking change, state clearly whether and how the
      project uses the affected API, citing the specific files/lines you read,
      and whether any assumption the code makes about it no longer holds. If the
      API is genuinely unused, say the project is NOT impacted and why.
    - 'risk_level': base it on the worst outcome the breaking change can cause in
      this project on realistic inputs. A change that can break the project at
      build time or at run time is at least HIGH; reserve LOW/MODERATE for
      changes that cannot break it either way.
    - 'verdict'/'summary': DO_NOT_APPROVE whenever the project relies on the
      affected API in a way the breaking change makes incorrect or unsafe — at
      build OR run time, including inputs outside the tests; otherwise APPROVE.
    - 'reasoning': fold the impact findings into the verdict rationale.
    - Keep 'breaking_changes', 'changelog_summary', and 'dependencies' populated.
    - Leave 'recommendations' empty. Proposing a fix is a separate step and must
      never influence this verdict.
""")

FIX_AGENT_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a senior Go engineer fixing a Dependabot pull request for the
    cloud-api-adaptor project. A prior impact analysis already established that
    THIS project is impacted by a breaking change and must be adapted before the
    update can be merged. Your job is to produce the concrete code fix — the
    verdict is already decided and is not yours to change.

    You can call read_file to read any source file you need so your patch matches
    the real code exactly. Read the actual lines first; do not guess.

    When ready, call submit_assessment with:
    - 'recommendations': at least one concrete fix. Each entry needs file, a short
      description, old_code copied VERBATIM from the source you read (so it
      applies cleanly), and new_code. Match the existing code character-for-
      character, including indentation.
    - 'verdict': DO_NOT_APPROVE, and reuse the prior 'summary', 'impact', and
      'reasoning'. The fix shows how to resolve the breakage; it does not approve
      the PR.
    - Keep 'breaking_changes', 'changelog_summary', and 'dependencies' populated.
""")


def run_cmd(cmd, check=True, cwd=None):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=cwd)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def run_gh(*args):
    return run_cmd(["gh", *args])


def run_git(*args, check=True, cwd=None):
    return run_cmd(["git", *args], check=check, cwd=cwd)


def checkout_pr(pr_number):
    """Check out a PR branch, resetting any stale local branch.

    Dependabot force-pushes its branches, so a local branch left over from a
    previous run can diverge from the remote and make a plain checkout fail
    with "Not possible to fast-forward". --force resets the local branch to
    match the PR's current head.
    """
    run_gh("pr", "checkout", str(pr_number), "--force")


def extract_pr_number(pr_arg):
    m = re.search(r'/pull/(\d+)', pr_arg)
    if m:
        return int(m.group(1))
    try:
        return int(pr_arg)
    except ValueError:
        print(f"ERROR: cannot parse PR number from: {pr_arg}", file=sys.stderr)
        sys.exit(1)


def get_current_gh_user():
    return run_cmd(["gh", "api", "user", "--jq", ".login"])


def fetch_pr_metadata(pr_number):
    fields = ("title,body,files,commits,author,labels,headRefName,baseRefName,"
              "state,reviews,mergeable,mergeStateStatus")
    raw = run_gh("pr", "view", str(pr_number), "--json", fields)
    return json.loads(raw)


def get_mergeable_status(pr_number, pr_data, retries=3, delay=2):
    """Return (mergeable, mergeStateStatus) for the PR.

    GitHub computes mergeability asynchronously, so a freshly-viewed PR may
    report "UNKNOWN". Poll a few times to give GitHub a chance to settle.
    """
    mergeable = pr_data.get("mergeable", "UNKNOWN")
    merge_state = pr_data.get("mergeStateStatus", "UNKNOWN")
    attempt = 0
    while mergeable == "UNKNOWN" and attempt < retries:
        time.sleep(delay)
        raw = run_gh("pr", "view", str(pr_number), "--json", "mergeable,mergeStateStatus")
        data = json.loads(raw)
        mergeable = data.get("mergeable", "UNKNOWN")
        merge_state = data.get("mergeStateStatus", "UNKNOWN")
        attempt += 1
    return mergeable, merge_state


def already_approved_by_user(pr_data, gh_user):
    for review in pr_data.get("reviews", []):
        if review.get("author", {}).get("login") == gh_user and review.get("state") == "APPROVED":
            return True
    return False


def determine_dep_type(pr_data):
    label_names = {l["name"] for l in pr_data.get("labels", [])}
    file_paths = [f["path"] for f in pr_data.get("files", [])]

    if "go" in label_names or any(p.endswith("go.mod") or p.endswith("go.sum") for p in file_paths):
        return "go"
    if "github_actions" in label_names or any(".github/workflows/" in p for p in file_paths):
        return "github_actions"
    return "unknown"


def parse_updated_dependencies(pr_data):
    """Extract dependency update info from the commit body's YAML block."""
    commits = pr_data.get("commits", [])
    if not commits:
        return []

    body = commits[0].get("messageBody", "")
    m = re.search(r'updated-dependencies:\s*\n(.*?)(?:\.\.\.|Signed-off-by:)', body, re.DOTALL)
    if not m:
        return []

    yaml_text = "updated-dependencies:\n" + m.group(1)
    try:
        parsed = yaml.safe_load(yaml_text)
        return parsed.get("updated-dependencies", [])
    except yaml.YAMLError:
        return []


def parse_version_updates(pr_data):
    """Extract old->new version mappings from the PR body and title."""
    updates = {}
    body = pr_data.get("body", "")
    for m in re.finditer(r'Updates?\s+`([^`]+)`\s+from\s+(\S+)\s+to\s+(\S+)', body):
        dep_name, old_ver, new_ver = m.group(1), m.group(2), m.group(3)
        if dep_name not in updates:
            updates[dep_name] = {"old": old_ver, "new": new_ver}

    title = pr_data.get("title", "")
    m = re.search(r'bump\s+(\S+)\s+from\s+(\S+)\s+to\s+(\S+)', title, re.IGNORECASE)
    if m:
        dep_name, old_ver, new_ver = m.group(1), m.group(2), m.group(3)
        if dep_name not in updates:
            updates[dep_name] = {"old": old_ver, "new": new_ver}

    return updates


def classify_risk(update_type):
    if not update_type:
        return "UNKNOWN"
    if "semver-major" in update_type:
        return "HIGH"
    if "semver-minor" in update_type:
        return "MODERATE"
    if "semver-patch" in update_type:
        return "LOW"
    return "UNKNOWN"


def extract_changelog_urls(body):
    """Extract release notes/changelog/commits URLs from the PR body."""
    urls = {}
    for label, pattern in [
        ("release_notes", r'\[Release notes\]\(([^)]+)\)'),
        ("changelog", r'\[Changelog\]\(([^)]+)\)'),
        ("commits", r'\[Commits\]\(([^)]+)\)'),
    ]:
        for m in re.finditer(pattern, body):
            url = m.group(1)
            urls.setdefault(label, set()).add(url)
    return {k: list(v) for k, v in urls.items()}


def fetch_url_content(url, max_chars=8000):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "review-dependabot-prs/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        return content[:max_chars]
    except Exception as e:
        return f"(failed to fetch {url}: {e})"


def _html_to_text(fragment):
    """Convert an HTML fragment from a PR body into readable plain text."""
    text = re.sub(r'(?is)<(script|style).*?</\1>', '', fragment)
    text = re.sub(r'(?i)<li[^>]*>', '\n- ', text)
    text = re.sub(r'(?i)<(br|/p|/h[1-6]|/tr|/blockquote|/ul|/ol)\s*/?>', '\n', text)
    text = re.sub(r'(?s)<[^>]+>', '', text)  # strip any remaining tags
    text = html.unescape(text)
    # Collapse runs of blank lines and trailing whitespace.
    out = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def extract_inline_changelogs(pr_body, max_section=6000, max_total=12000):
    """Extract changelog text embedded inline in the PR body.

    Dependabot embeds release notes / changelog / commits directly in the PR
    body as ``<details><summary>LABEL</summary>...</details>`` blocks, so the
    content is already present and can be parsed without any network fetch.
    Only the release-notes and changelog sections are kept (commit lists are
    noisy and add little review value). Returns an empty string if the body
    has no such blocks (e.g. a markdown-link style body).
    """
    blocks = []
    total = 0
    for m in re.finditer(
        r'(?is)<details>\s*<summary>(.*?)</summary>(.*?)</details>', pr_body
    ):
        label = _html_to_text(m.group(1))
        if not re.search(r'(?i)release notes|changelog', label):
            continue
        content = _html_to_text(m.group(2))
        if not content:
            continue
        if len(content) > max_section:
            content = content[:max_section] + "\n... (truncated)"
        block = f"### {label}\n{content}"
        blocks.append(block)
        total += len(block)
        if total >= max_total:
            blocks.append("... (changelog context truncated)")
            break
    return "\n\n".join(blocks)


def build_analysis_report(pr_number, pr_data, dep_type, deps, version_updates):
    """Build the deterministic analysis report."""
    lines = []
    lines.append(f"## 1. Dependency Update Analysis")
    lines.append("")
    lines.append(f"PR #{pr_number}: {pr_data['title']}")
    lines.append(f"Type: {'Go modules' if dep_type == 'go' else 'GitHub Actions' if dep_type == 'github_actions' else 'Unknown'}")
    lines.append(f"Branch: {pr_data['headRefName']} -> {pr_data['baseRefName']}")
    lines.append("")

    seen = set()
    lines.append("Dependencies:")
    for dep in deps:
        name = dep.get("dependency-name", "unknown")
        dep_key = (name, dep.get("dependency-version", ""))
        if dep_key in seen:
            continue
        seen.add(dep_key)

        new_ver = dep.get("dependency-version", "?")
        update_type = dep.get("update-type", "")
        dep_type_label = dep.get("dependency-type", "unknown")
        risk = classify_risk(update_type)

        semver_label = update_type.replace("version-update:semver-", "") if update_type else "unknown"
        old_ver = version_updates.get(name, {}).get("old", "?")

        lines.append(f"  - {name}: {old_ver} -> {new_ver} ({semver_label}, {risk} risk, {dep_type_label})")

    lines.append("")
    lines.append("Modified files:")
    for f in pr_data.get("files", []):
        lines.append(f"  - {f['path']} (+{f['additions']}/-{f['deletions']})")

    return "\n".join(lines)


def run_go_mod_tidy(pr_number, repo_root, dry_run=False):
    """Run go mod tidy and return whether it produced changes."""
    if dry_run:
        print("\n## 2. go mod tidy")
        print("Skipped (dry-run mode)")
        return False

    print(f"\nChecking out PR #{pr_number}...")
    checkout_pr(pr_number)

    print("Running ./hack/go-tidy.sh...")
    tidy_script = os.path.join(repo_root, "hack", "go-tidy.sh")
    # go-tidy.sh discovers modules with `find . -name go.mod`, so it must run
    # from the repo root — otherwise (e.g. when launched from hack/) it finds
    # no modules and silently tidies nothing.
    run_cmd([tidy_script], cwd=repo_root)

    diff_stat = run_git("diff", "--stat")
    print("\n## 2. go mod tidy")
    if diff_stat:
        print("Changes detected:")
        print(diff_stat)
        run_git("add", "-A")
        # Build a commit message that satisfies the repo's commit-message-check:
        # a "subsystem: summary" subject plus a body. Sign off (-s) so it passes
        # the DCO check. No AI is involved here (this is deterministic go-tidy.sh
        # output), so there is no Assisted-by trailer.
        subject = "build(deps): go mod tidy"
        body = ("Ran hack/go-tidy.sh to sync the inter-module dependencies that\n"
                "the Dependabot version bump left out of tidy.")
        run_git("commit", "-s", "-m", subject, "-m", body)
        print(f"Committed locally as '{subject}'. NOT pushed — push manually if needed.")
        return True
    else:
        print("No changes produced.")
        return False


def read_repo_file(repo_root, rel_path, max_bytes=60000):
    """Read a repo-relative file for the model, refusing paths outside the repo."""
    root = os.path.realpath(repo_root)
    full = os.path.realpath(os.path.join(root, rel_path))
    if full != root and not full.startswith(root + os.sep):
        return f"(refused: path is outside the repository: {rel_path})"
    if not os.path.isfile(full):
        return f"(not found: {rel_path})"
    with open(full, errors="replace") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        data = data[:max_bytes] + "\n... (truncated)"
    return data


def find_importing_files(module_paths, repo_root, max_files=40):
    """List repo-relative .go files that reference any of the given modules."""
    files = set()
    for module in module_paths:
        if not module:
            continue
        result = subprocess.run(
            ["grep", "-rl", "--include=*.go", "--exclude-dir=vendor", module, "."],
            capture_output=True, text=True, timeout=30, cwd=repo_root
        )
        for line in result.stdout.strip().splitlines():
            if line:
                files.add(line[2:] if line.startswith("./") else line)
    return sorted(files)[:max_files]


def gather_changelog_context(pr_body):
    """Gather changelog content for the AI assessment.

    Prefer the changelog text Dependabot embeds inline in the PR body; only
    fall back to fetching the linked URLs for bodies that use markdown links
    instead of inline <details> blocks.
    """
    inline = extract_inline_changelogs(pr_body)
    if inline:
        return inline

    urls = extract_changelog_urls(pr_body)
    parts = []
    fetched = set()
    for label in ["release_notes", "changelog", "commits"]:
        for url in urls.get(label, []):
            if url in fetched:
                continue
            fetched.add(url)
            content = fetch_url_content(url)
            parts.append(f"### {label}: {url}\n{content}\n")
            if len(fetched) >= 5:
                break
        if len(fetched) >= 5:
            break
    return "\n".join(parts) if parts else "(no changelogs fetched)"


MAX_TOKENS = 4096

# Agentic-loop tools. read_file lets the model browse the project sources on
# demand; submit_assessment returns the final structured review. The names are
# kept identical to the ones referenced in the impact/fix system prompts.
_READ_FILE_DESC = (
    "Read a source file from the project to inspect how it uses the changed "
    "dependency. Follow references into other files as needed."
)
_READ_FILE_PATH_DESC = (
    "Repo-relative path, e.g. src/cloud-providers/ibmcloud/provider.go"
)


class _LangChainAssessor:
    """Wraps a LangChain chat model that supports tool calling."""

    def __init__(self, chat):
        self._chat = chat
        # Use tool/function calling rather than the json_schema response format:
        # the latter is OpenAI's default and emits a pydantic serialization
        # warning about its 'parsed' field.
        self._structured = chat.with_structured_output(
            Assessment, method="function_calling")

    def assess(self, system_msg, user_msg):
        from langchain_core.messages import SystemMessage, HumanMessage
        return self._structured.invoke(
            [SystemMessage(content=system_msg), HumanMessage(content=user_msg)])

    def assess_agentic(self, system_msg, user_msg, repo_root):
        """Run an agentic assessment letting the model read project files.

        Provider-agnostic twin of _VertexAssessor.assess_agentic, built on
        LangChain tool calling so anthropic and openai behave identically. The
        model calls read_file to inspect any source, then submit_assessment.
        The loop ends when it submits; if it ever replies without calling a
        tool we force a final submit so it always terminates without a counter.
        Used for both the impact pass and the (opt-in) fix pass — the behavior
        is identical; only the system/user prompts differ.
        """
        from langchain_core.messages import (
            SystemMessage, HumanMessage, ToolMessage)

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": _READ_FILE_DESC,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string",
                                     "description": _READ_FILE_PATH_DESC},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "submit_assessment",
                    "description": "Submit the final structured review assessment.",
                    "parameters": Assessment.model_json_schema(),
                },
            },
        ]
        llm = self._chat.bind_tools(tools)
        forced = self._chat.bind_tools(
            tools, tool_choice="submit_assessment")
        messages = [SystemMessage(content=system_msg),
                    HumanMessage(content=user_msg)]

        while True:
            ai = llm.invoke(messages)
            messages.append(ai)
            tool_calls = ai.tool_calls or []

            if not tool_calls:
                # The model replied without calling a tool; force the submit.
                ai = forced.invoke(messages)
                messages.append(ai)
                tool_calls = ai.tool_calls or []

            made_progress = False
            for tc in tool_calls:
                if tc["name"] == "submit_assessment":
                    return Assessment.model_validate(tc["args"])
                if tc["name"] == "read_file":
                    path = tc["args"].get("path", "")
                    print(f"  read_file: {path}")
                    messages.append(ToolMessage(
                        content=read_repo_file(repo_root, path),
                        tool_call_id=tc["id"]))
                    made_progress = True

            if not made_progress:
                raise RuntimeError("model did not return a structured assessment")


class _VertexAssessor:
    """Claude on Google Cloud Vertex AI via the anthropic SDK.

    Authenticates with Application Default Credentials (gcloud auth
    application-default login) instead of an API key, and uses forced tool
    use to return a structured Assessment. This avoids the heavy
    langchain-google-vertexai / aiplatform dependency stack.
    """

    def __init__(self, model_name, project, location):
        from anthropic import AnthropicVertex
        self._client = AnthropicVertex(project_id=project, region=location)
        self._model = model_name

    def assess(self, system_msg, user_msg):
        tool = {
            "name": "submit_assessment",
            "description": "Submit the structured review assessment.",
            "input_schema": Assessment.model_json_schema(),
        }
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=system_msg,
            messages=[{"role": "user", "content": user_msg}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "submit_assessment"},
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_assessment":
                return Assessment.model_validate(block.input)
        raise RuntimeError("model did not return a structured assessment")

    def assess_agentic(self, system_msg, user_msg, repo_root):
        """Run an agentic assessment letting the model read project files.

        The model may call read_file to inspect any source file it needs, then
        calls submit_assessment. The loop ends when it submits; if it ever
        replies without calling a tool, we force a final submit so the loop
        always terminates without needing an iteration counter. Used for both
        the impact pass and the (opt-in) fix pass — the behavior is identical;
        only the system/user prompts differ.
        """
        read_file = {
            "name": "read_file",
            "description": _READ_FILE_DESC,
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": _READ_FILE_PATH_DESC},
                },
                "required": ["path"],
            },
        }
        submit = {
            "name": "submit_assessment",
            "description": "Submit the final structured review assessment.",
            "input_schema": Assessment.model_json_schema(),
        }
        tools = [read_file, submit]
        messages = [{"role": "user", "content": user_msg}]

        while True:
            resp = self._client.messages.create(
                model=self._model, max_tokens=MAX_TOKENS, system=system_msg,
                messages=messages, tools=tools,
            )
            messages.append({"role": "assistant", "content": resp.content})

            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                if block.name == "submit_assessment":
                    return Assessment.model_validate(block.input)
                if block.name == "read_file":
                    path = block.input.get("path", "")
                    print(f"  read_file: {path}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": read_repo_file(repo_root, path),
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                continue

            # The model replied without reading a file or submitting; force it
            # to produce the final assessment now.
            resp = self._client.messages.create(
                model=self._model, max_tokens=MAX_TOKENS, system=system_msg,
                messages=messages, tools=tools,
                tool_choice={"type": "tool", "name": "submit_assessment"},
            )
            messages.append({"role": "assistant", "content": resp.content})
            for block in resp.content:
                if block.type == "tool_use" and block.name == "submit_assessment":
                    return Assessment.model_validate(block.input)
            raise RuntimeError("model did not return a structured assessment")


def create_model(provider, model_name):
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return _LangChainAssessor(ChatAnthropic(model=model_name, max_tokens=MAX_TOKENS))
    elif provider == "vertex":
        project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
        location = os.environ.get("CLOUD_ML_REGION")
        if not project:
            raise RuntimeError(
                "provider 'vertex' requires ANTHROPIC_VERTEX_PROJECT_ID to be set")
        if not location:
            raise RuntimeError(
                "provider 'vertex' requires CLOUD_ML_REGION to be set")
        return _VertexAssessor(model_name, project, location)
    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        return _LangChainAssessor(ChatOpenAI(model=model_name, max_tokens=MAX_TOKENS))
    else:
        raise RuntimeError(f"unsupported provider: {provider}")


def get_ai_assessment(model, analysis_report, changelog_context, diff_context):
    """Build the prompt and return a structured Assessment from the model."""
    user_msg = "\n".join([
        "# Analysis Report",
        analysis_report,
        "",
        "# Changelog / Release Notes",
        changelog_context,
        "",
        "# PR Diff",
        diff_context,
    ])

    print("\nCalling AI model for assessment...")
    return model.assess(SYSTEM_PROMPT, user_msg)


def get_impact_assessment_agentic(model, analysis_report, changelog_context,
                                  diff_context, first_pass, candidate_files,
                                  repo_root, want_fix):
    """Second-pass, breaking-change impact analysis (and optional fix).

    The verdict is decided by the impact pass ALONE — same prompt regardless of
    want_fix — so asking for a fix can never change whether the PR is approved.
    A fix is a strictly downstream, opt-in step that only runs when want_fix is
    set AND the project is impacted (DO_NOT_APPROVE); it produces the patch but
    leaves the verdict untouched. An APPROVE therefore never carries a fix.
    """
    breaking = "\n".join(
        f"- {bc.dependency}: {bc.api} — {bc.description} "
        f"(symbols: {', '.join(bc.symbols) or 'none'})"
        for bc in first_pass.breaking_changes
    )
    files = "\n".join(f"- {f}" for f in candidate_files) or "(none found)"

    impact_msg = "\n".join([
        "# Analysis Report",
        analysis_report,
        "",
        "# Breaking changes identified in the first pass",
        breaking,
        "",
        "# Files that reference the changed dependency (starting point)",
        files,
        "",
        "# Changelog / Release Notes",
        changelog_context,
        "",
        "# PR Diff",
        diff_context,
    ])

    print("Breaking changes found — inspecting project sources for impact...")
    assessment = model.assess_agentic(
        IMPACT_AGENT_SYSTEM_PROMPT, impact_msg, repo_root)
    # The verdict pass never proposes code changes; drop anything volunteered.
    assessment.recommendations = []

    if not (want_fix and assessment.verdict == "DO_NOT_APPROVE"):
        return assessment

    # Downstream fix pass: the project is impacted and the user asked for a fix.
    # It only fills in recommendations; the verdict above is authoritative.
    fix_msg = "\n".join([
        "# Impact analysis (already decided: the project IS impacted)",
        assessment.impact or assessment.reasoning,
        "",
        "# Breaking changes",
        breaking,
        "",
        "# Files that reference the changed dependency (starting point)",
        files,
        "",
        "# PR Diff",
        diff_context,
    ])

    print("Impacted — generating a concrete fix...")
    fix = model.assess_agentic(FIX_AGENT_SYSTEM_PROMPT, fix_msg, repo_root)
    assessment.recommendations = fix.recommendations
    if not fix.recommendations:
        print("  (model produced no concrete fix)")
    return assessment


def render_assessment(assessment):
    """Render a human-readable summary of an Assessment."""
    lines = [
        f"Verdict:    {assessment.verdict}",
        f"Risk level: {assessment.risk_level}",
        "",
        f"Summary: {assessment.summary}",
        "",
        "Changelog summary:",
        assessment.changelog_summary,
    ]
    if assessment.dependencies:
        lines += ["", "Dependencies:"]
        for dep in assessment.dependencies:
            lines.append(f"  - {dep.name}: {dep.change} ({dep.risk} risk)")
            lines.append(f"      {dep.notes}")
    if assessment.breaking_changes:
        lines += ["", "Breaking changes:"]
        for bc in assessment.breaking_changes:
            lines.append(f"  - {bc.dependency}: {bc.api}")
            lines.append(f"      {bc.description}")
            if bc.symbols:
                lines.append(f"      symbols: {', '.join(bc.symbols)}")
    if assessment.impact:
        lines += ["", "Impact on this project:", assessment.impact]
    lines += ["", "Reasoning:", assessment.reasoning]
    if assessment.recommendations:
        lines += ["", f"Fixes ({len(assessment.recommendations)}):"]
        for rec in assessment.recommendations:
            lines.append(f"  - {rec.file}: {rec.description}")
    return "\n".join(lines)


def build_recommendation_commit(rec, assisted_by):
    """Build a (subject, body) that satisfies the repo's commit-message check.

    The subject is 'subsystem: summary' (subsystem derived from the file's
    directory) capped at 72 chars; the body explains the change, wrapped at 72
    columns, with an Assisted-by trailer since the fix is AI-generated.
    """
    subsystem = os.path.basename(os.path.dirname(rec.file)) or "deps"
    summary = " ".join(rec.description.strip().split())
    summary = summary[0].lower() + summary[1:] if summary else "apply fix"
    summary = summary.rstrip(".")
    subject = f"{subsystem}: {summary}"
    if len(subject) > 72:
        subject = subject[:71].rstrip() + "…"

    body_text = " ".join(rec.description.strip().split())
    body = textwrap.fill(body_text, width=72)
    body += (
        "\n\nApplied automatically by review_dependabot_prs.py to adapt the "
        "project sources to the bumped dependency.")
    body = "\n".join(textwrap.fill(line, width=72) if line else line
                     for line in body.splitlines())
    body += f"\n\nAssisted-by: {assisted_by}"
    return subject, body


def apply_recommendations(recommendations, repo_root, pr_branch, assisted_by):
    """Apply the fix recommendations as individual commits on a new branch."""
    branch_name = f"{pr_branch}-fixes"
    # Pathspecs in rec.file are repo-relative, so run git from the repo root
    # (the script may be launched from hack/ or any subdirectory).
    run_git("checkout", "-b", branch_name, cwd=repo_root)

    applied = 0
    for rec in recommendations:
        filepath = os.path.join(repo_root, rec.file)
        if not os.path.isfile(filepath):
            print(f"  WARNING: file not found: {rec.file}, skipping")
            continue

        with open(filepath) as f:
            content = f.read()

        if rec.old_code not in content:
            print(f"  WARNING: old_code not found in {rec.file}, skipping: {rec.description}")
            continue

        new_content = content.replace(rec.old_code, rec.new_code, 1)
        with open(filepath, "w") as f:
            f.write(new_content)

        subject, body = build_recommendation_commit(rec, assisted_by)
        run_git("add", rec.file, cwd=repo_root)
        run_git("commit", "-s", "-m", subject, "-m", body, cwd=repo_root)
        applied += 1
        print(f"  Applied: {rec.description}")

    print(f"\n{applied} fix(es) applied on branch '{branch_name}'.")
    if applied > 0:
        print("NOT pushed — push manually if needed.")
    return applied


def process_pr(pr_number, args, model, original_branch, repo_root):
    """Process a single Dependabot PR through the full pipeline.

    Returns a result dict summarizing the outcome for the end-of-run report:
      number, url, status, note, verdict, risk.
    """
    pr_url = f"https://github.com/{REPO}/pull/{pr_number}"

    def result(status, note="", verdict=None, risk=None):
        return {"number": pr_number, "url": pr_url, "status": status,
                "note": note, "verdict": verdict, "risk": risk}

    print(f"\n{'='*60}")
    print(f"Processing PR #{pr_number}")
    print(f"{'='*60}")

    # Step 1: Fetch PR data
    pr_data = fetch_pr_metadata(pr_number)

    if pr_data["author"]["login"] != "app/dependabot":
        print(f"WARNING: PR #{pr_number} is not from Dependabot (author: {pr_data['author']['login']}), skipping.")
        return result("skipped", "not from Dependabot")

    if pr_data["state"] != "OPEN":
        print(f"WARNING: PR #{pr_number} is not open (state: {pr_data['state']}), skipping.")
        return result("skipped", f"state is {pr_data['state']}")

    if already_approved_by_user(pr_data, args.gh_user):
        print(f"Skipping PR #{pr_number} — already approved by {args.gh_user}.")
        return result("skipped", f"already approved by {args.gh_user}")

    # Step 2: Parse updates
    dep_type = determine_dep_type(pr_data)
    deps = parse_updated_dependencies(pr_data)
    version_updates = parse_version_updates(pr_data)

    # Step 3: Print deterministic analysis
    report = build_analysis_report(pr_number, pr_data, dep_type, deps, version_updates)
    print(f"\n{report}")

    # Step 3.5: Mergeability / conflict check
    mergeable, merge_state = get_mergeable_status(pr_number, pr_data)
    has_conflict = mergeable == "CONFLICTING"
    if has_conflict:
        print(f"\n## Merge Conflict")
        print(f"WARNING: PR #{pr_number} conflicts with '{pr_data['baseRefName']}' "
              f"(mergeable={mergeable}, mergeStateStatus={merge_state}).")
        print("This PR must be rebased before it can be processed. "
              "go mod tidy and auto-approval will be skipped.")
    elif mergeable == "UNKNOWN":
        print(f"\nNote: GitHub has not finished computing mergeability for PR "
              f"#{pr_number} (mergeable=UNKNOWN); proceeding but a conflict "
              f"may surface later.")

    # Step 4: go mod tidy (opt-in)
    tidy_produced_changes = False
    checked_out = False
    if args.tidy:
        if has_conflict:
            print("\n## 2. go mod tidy")
            print("Skipped — PR has merge conflicts; rebase required.")
        elif dep_type == "go":
            tidy_produced_changes = run_go_mod_tidy(pr_number, repo_root, dry_run=args.dry_run)
            checked_out = not args.dry_run
        else:
            print("\n## 2. go mod tidy")
            print("Skipped — not a Go module update.")
    else:
        print("\n## 2. go mod tidy")
        print("Skipped (--tidy not set)")

    # Step 5: AI assessment (always runs unless dry-run)
    if args.dry_run:
        print("\n## 3. AI Assessment")
        print("Skipped (dry-run mode)")
        return result("skipped", "dry-run mode")

    # Gather context for AI
    changelog_context = gather_changelog_context(pr_data.get("body", ""))

    # Fetch PR diff for context
    pr_diff = run_gh("pr", "diff", str(pr_number))
    if len(pr_diff) > 10000:
        pr_diff = pr_diff[:10000] + "\n... (diff truncated)"
    diff_context = f"```\n{pr_diff}\n```"

    assessment = get_ai_assessment(
        model, report, changelog_context, diff_context)

    # Step 5.5: Breaking-change impact analysis. If the first pass found any
    # breaking changes, run a second pass that judges the real impact on this
    # project. The model browses the sources itself via read_file, seeded with
    # the files that reference the changed dependency. When --fix (or --apply)
    # is set it also produces concrete fixes.
    if dep_type == "go" and assessment.breaking_changes:
        modules = {bc.dependency for bc in assessment.breaking_changes}
        candidate_files = find_importing_files(modules, repo_root)
        assessment = get_impact_assessment_agentic(
            model, report, changelog_context, diff_context,
            assessment, candidate_files, repo_root, want_fix=args.fix)

    # Print AI assessment
    print(f"\n## 3. AI Assessment")
    print(render_assessment(assessment))

    # Step 6: Apply fixes (opt-in)
    applied_count = 0
    if args.apply and assessment.recommendations:
        if not checked_out:
            checkout_pr(pr_number)
            checked_out = True

        print(f"\n## 4. Applying Fixes")
        applied_count = apply_recommendations(
            assessment.recommendations, repo_root, pr_data["headRefName"],
            f"{args.provider}({args.model})"
        )

    # Determine the outcome status for the end-of-run summary. Merge conflicts
    # and pending tidy changes are flagged even when --approve is not set so the
    # user knows those PRs need attention before they can be merged.
    status = "reviewed"
    note = ""
    if has_conflict:
        status = "conflict"
        note = "merge conflict — needs rebase"
    elif tidy_produced_changes:
        status = "tidy-changes"
        note = "go mod tidy produced changes — needs push"

    # Step 7: Execute approval (opt-in)
    if args.approve:
        print(f"\n## {'5' if args.apply else '4'}. Auto-Approve")
        if has_conflict:
            print("BLOCKED: PR has merge conflicts; rebase required before approval.")
        elif tidy_produced_changes:
            print("BLOCKED: go mod tidy produced changes that need to be pushed first.")
        elif assessment.verdict == "APPROVE":
            approval_body = (
                "Auto-approved by review_dependabot_prs.py.\n"
                f"Analysis summary by {args.provider}({args.model}):\n\n"
                f"{assessment.summary}"
            )
            run_gh("pr", "review", str(pr_number), "--approve", "--body", approval_body)
            print(f"PR #{pr_number} approved.")
            status = "auto-approved"
            note = ""
        else:
            print(f"NOT approved. Reason: {assessment.summary}\n{assessment.reasoning}")
            status = "not-approved"

    # Step 8: Cleanup
    if checked_out and not tidy_produced_changes and applied_count == 0:
        current_branch = run_git("branch", "--show-current")
        if current_branch != original_branch:
            pr_branch = pr_data["headRefName"]
            run_git("checkout", original_branch)
            run_git("branch", "-D", pr_branch, check=False)

    return result(status, note, assessment.verdict, assessment.risk_level)


def print_summary(results):
    """Print an end-of-run summary of scanned PRs for easy manual triage."""
    if not results:
        return

    # Human-readable label per status.
    labels = {
        "auto-approved": "AUTO-APPROVED",
        "reviewed": "REVIEW",
        "not-approved": "NOT APPROVED",
        "conflict": "CONFLICT",
        "tidy-changes": "TIDY CHANGES",
        "skipped": "SKIPPED",
        "error": "ERROR",
    }

    print(f"\n{'='*60}")
    print(f"Summary — {len(results)} PR(s) scanned")
    print(f"{'='*60}")

    for r in results:
        label = labels.get(r["status"], r["status"].upper())
        verdict = r.get("verdict") or "-"
        risk = r.get("risk") or "-"
        line = f"  #{r['number']:<6} {label:<14} verdict={verdict:<14} risk={risk:<9} {r['url']}"
        print(line)
        if r.get("note"):
            print(f"          ↳ {r['note']}")


def main():
    parser = argparse.ArgumentParser(
        description="Review Dependabot pull requests with AI-assisted analysis."
    )

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=str, help="PR number or URL")
    target.add_argument("--scan", action="store_true",
                        help="Scan all open Dependabot PRs")

    parser.add_argument("--tidy", action="store_true",
                        help="Run go mod tidy (Go module PRs only)")
    parser.add_argument("--fix", action="store_true",
                        help="On a breaking change, generate and print a concrete "
                             "code fix (no commit)")
    parser.add_argument("--apply", action="store_true",
                        help="Also commit the generated fix on a new branch "
                             "(implies --fix)")
    parser.add_argument("--approve", action="store_true",
                        help="Auto-approve PR if AI assessment passes")
    parser.add_argument("--model", type=str, default="claude-opus-4-8",
                        help="AI model name (default: claude-opus-4-8)")
    parser.add_argument("--provider", type=str,
                        choices=["anthropic", "vertex", "openai"],
                        default="vertex",
                        help="AI provider (default: vertex). 'anthropic' and "
                             "'openai' use an API key; 'vertex' uses Claude on "
                             "Google Cloud Vertex AI via ADC "
                             "(ANTHROPIC_VERTEX_PROJECT_ID / CLOUD_ML_REGION)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print analysis only, no checkout/approve/commit")

    args = parser.parse_args()

    if args.apply:
        args.fix = True

    repo_root = run_git("rev-parse", "--show-toplevel")
    original_branch = run_git("branch", "--show-current")
    args.gh_user = get_current_gh_user()

    model = None
    if not args.dry_run:
        model = create_model(args.provider, args.model)

    if args.pr:
        pr_numbers = [extract_pr_number(args.pr)]
    else:
        raw = run_gh("pr", "list", "--author", "app/dependabot", "--state", "open",
                     "--json", "number,title", "--limit", "50")
        prs = json.loads(raw)
        pr_numbers = [pr["number"] for pr in prs]
        if not pr_numbers:
            print("No open Dependabot PRs found.")
            return
        print(f"Found {len(pr_numbers)} open Dependabot PR(s):")
        for pr in prs:
            print(f"  #{pr['number']}: {pr['title']}")

    results = []
    for pr_number in pr_numbers:
        try:
            res = process_pr(pr_number, args, model, original_branch, repo_root)
            if res:
                results.append(res)
        except Exception as e:
            print(f"\nERROR processing PR #{pr_number}: {e}", file=sys.stderr)
            results.append({
                "number": pr_number,
                "url": f"https://github.com/{REPO}/pull/{pr_number}",
                "status": "error",
                "note": str(e).splitlines()[0],
                "verdict": None,
                "risk": None,
            })
            try:
                run_git("checkout", original_branch, check=False)
            except Exception:
                pass

    print_summary(results)

    current = run_git("branch", "--show-current")
    if current != original_branch:
        print(f"\nNote: you are on branch '{current}', not '{original_branch}'.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
