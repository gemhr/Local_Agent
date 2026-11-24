"""为受信合成仓库核对磁盘变更并生成可冻结的单文件 Patch。"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import TemporaryDirectory

from core.agent_platform.ci_diagnosis_tools import strict_object
from core.agent_platform.workflow_artifacts import WorkflowArtifactError, _lstat
from core.ci_simulation import tlv_input
from core.incidents.repair_input import RepairInput
from core.runtime.snapshot_serialization import sha256_digest, text_digest

SCHEMA = "ci-repair-artifact.v1"
TARGET = tlv_input.SCRIPT_PATHS[tlv_input.PRIMARY_CASE]
MAX_FILE_BYTES = 16384


def _git(root, *args, allow_difference=False, input_bytes=None):
    # 禁用宿主 Git 配置、fsmonitor 及可选索引锁。
    env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    if input_bytes is not None:
        env.update(GIT_CEILING_DIRECTORIES=str(root.parent), GIT_ATTR_NOSYSTEM="1")
    try:
        result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            input=input_bytes, env=env, capture_output=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkflowArtifactError("CI_REPAIR_GIT_UNAVAILABLE") from None
    if result.returncode != 0 and not (allow_difference and result.returncode == 1):
        raise WorkflowArtifactError("CI_REPAIR_GIT_INVALID")
    return result


def _identity(info):
    return [info.st_dev, info.st_ino]


def _read(path, limit):
    before = _lstat(path, stat.S_ISREG)
    if before.st_nlink != 1 or before.st_size > limit:
        raise WorkflowArtifactError("CI_REPAIR_FILE_INVALID")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
        opened = os.fstat(handle.fileno())
    after = _lstat(path, stat.S_ISREG)
    fingerprint = lambda s: (_identity(s), s.st_size, s.st_mtime_ns, s.st_mode, s.st_nlink)
    if len(raw) != before.st_size or fingerprint(before) != fingerprint(opened) or fingerprint(before) != fingerprint(after):
        raise WorkflowArtifactError("CI_REPAIR_FILE_CHANGED")
    return raw, stat.S_IMODE(before.st_mode)


def _git_metadata(path):
    """三个文件的小仓库只允许原有 Git 元数据，包含索引、引用及配置。"""
    items = {}
    for directory, dirs, files in os.walk(path, followlinks=False):
        parent = Path(directory)
        for name in dirs:
            child = parent / name
            _lstat(child, stat.S_ISDIR)
            items[child.relative_to(path).as_posix() + "/"] = "dir"
        for name in files:
            child = parent / name
            raw, mode = _read(child, 1024 * 1024)
            items[child.relative_to(path).as_posix()] = [hashlib.sha256(raw).hexdigest(), mode]
        if len(items) > 128:
            raise WorkflowArtifactError("CI_REPAIR_GIT_INVALID")
    if {"commondir", "objects/info/alternates", "config.worktree"} & items.keys():
        raise WorkflowArtifactError("CI_REPAIR_GIT_INVALID")
    return sha256_digest(items)


def _observe(path, baseline=None):
    try:
        root = Path(path)
        if not root.is_absolute() or root.drive.startswith("\\\\") or ".." in root.parts:
            raise WorkflowArtifactError("CI_REPAIR_REPOSITORY_INVALID")
        for part in (*reversed(root.parents), root, root / "cases", root / ".git"):
            _lstat(part, stat.S_ISDIR)
        if {p.name for p in root.iterdir()} != {".git", "cases", tlv_input.INTERFACE_PATH} or {
            p.name for p in (root / "cases").iterdir()
        } != {Path(p).name for p in tlv_input.SCRIPT_PATHS.values()}:
            raise WorkflowArtifactError("CI_REPAIR_WORKTREE_SCOPE_INVALID")
        root_id = _identity(_lstat(root, stat.S_ISDIR))
        metadata = _git_metadata(root / ".git")
        if baseline is not None and (root_id != baseline["root_id"] or metadata != baseline["git_digest"]):
            raise WorkflowArtifactError("CI_REPAIR_BASELINE_DRIFT")
        reported_root = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").stdout.strip()))
        if reported_root != root or _git(root, "remote").stdout:
            raise WorkflowArtifactError("CI_REPAIR_REPOSITORY_INVALID")
        head, tree = _git(root, "rev-parse", "HEAD", "HEAD^{tree}").stdout.decode("ascii").splitlines()
        if any(not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", ref) for ref in (head, tree)):
            raise WorkflowArtifactError("CI_REPAIR_GIT_INVALID")
        tracked = _git(root, "ls-tree", "-r", "-z", "HEAD").stdout.rstrip(b"\0").split(b"\0")
        manifest = {name.decode("ascii"): value for value, name in (entry.split(b"\t", 1) for entry in tracked)}
        if manifest.keys() != tlv_input.BASELINE_DIGESTS.keys() or any(not v.startswith(b"100644 blob ") for v in manifest.values()):
            raise WorkflowArtifactError("CI_REPAIR_GIT_INVALID")
        if _git(root, "diff", "--cached", "--quiet", "HEAD", "--", allow_difference=True).returncode:
            raise WorkflowArtifactError("CI_REPAIR_INDEX_CHANGED")
        files, modes = {}, {}
        for name, digest in tlv_input.BASELINE_DIGESTS.items():
            original = _git(root, "show", "HEAD:" + name).stdout
            if hashlib.sha256(original).hexdigest() != digest:
                raise WorkflowArtifactError("CI_REPAIR_BASELINE_DRIFT")
            files[name], modes[name] = _read(root / name, MAX_FILE_BYTES)
            if (baseline is None or name != TARGET) and files[name] != original:
                raise WorkflowArtifactError("CI_REPAIR_WORKTREE_SCOPE_INVALID")
        observed = dict(root_id=root_id, git_digest=metadata, revision=head, tree=tree, modes=modes,
            original_files=dict(tlv_input.BASELINE_DIGESTS))
        if baseline is not None and observed != baseline:
            raise WorkflowArtifactError("CI_REPAIR_BASELINE_DRIFT")
        if _git_metadata(root / ".git") != metadata:
            raise WorkflowArtifactError("CI_REPAIR_BASELINE_DRIFT")
        return observed, files
    except (OSError, ValueError) as exc:
        if isinstance(exc, WorkflowArtifactError):
            raise
        raise WorkflowArtifactError("CI_REPAIR_REPOSITORY_INVALID") from None


def capture_baseline(path, instance_id):
    observed, _ = _observe(path)
    return dict(**observed, root_path=str(Path(path)), runtime_instance_id=instance_id, allowed_paths=[TARGET])


def _baseline_details(baseline):
    return {k: baseline[k] for k in ("root_id", "git_digest", "revision", "tree", "modes", "original_files")}


def require_pristine(baseline):
    _, files = _observe(baseline["root_path"], _baseline_details(baseline))
    if hashlib.sha256(files[TARGET]).hexdigest() != tlv_input.BASELINE_DIGESTS[TARGET]:
        raise WorkflowArtifactError("CI_REPAIR_BASELINE_DRIFT")


def require_input(raw, digest):
    try:
        repair = RepairInput.model_validate_json(raw)
        if repair.canonical_bytes() != raw.encode("utf-8") or repair.digest() != digest:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise WorkflowArtifactError("CI_REPAIR_INPUT_INVALID") from None
    return repair


def _lf_text(raw):
    if b"\r" in raw:
        raise ValueError
    return raw.decode("utf-8")


def _verify_patch(before, patch, after, after_sha256):
    try:
        if not all(0 < len(raw) <= MAX_FILE_BYTES for raw in (before, patch, after)):
            raise ValueError
        _lf_text(before)
        _lf_text(after)
        with TemporaryDirectory(prefix="ci-repair-patch-") as directory:
            root = Path(directory)
            target = root / TARGET
            target.parent.mkdir()
            target.write_bytes(before)
            result = _git(root, "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                "-c", f"core.attributesFile={os.devnull}", "apply", "--numstat", "--summary", "-z", "--apply",
                "--whitespace=nowarn", "-", input_bytes=patch)
            # 只接受目标路径的一条内容变更记录，拒绝额外文件和模式等元数据变更。
            added, deleted, path = result.stdout.split(b"\t", 2)
            if not (added.isdigit() and deleted.isdigit() and path == TARGET.encode("ascii") + b"\0"):
                raise ValueError
            applied, _ = _read(target, MAX_FILE_BYTES)
            if applied != after or hashlib.sha256(applied).hexdigest() != after_sha256:
                raise ValueError
    except WorkflowArtifactError as exc:
        if exc.code == "CI_REPAIR_GIT_UNAVAILABLE":
            raise
        raise WorkflowArtifactError("CI_REPAIR_PATCH_INVALID") from None
    except (OSError, ValueError):
        raise WorkflowArtifactError("CI_REPAIR_PATCH_INVALID") from None


def freeze_patch(run, step, result_ref):
    baseline = run.repair_baseline
    root, expected = baseline["root_path"], _baseline_details(baseline)
    _, files = _observe(root, expected)
    original = _git(root, "show", "HEAD:" + TARGET).stdout
    try:
        before = _lf_text(original)
        after = _lf_text(files[TARGET])
        old, new = strict_object(before), strict_object(after)
        interface = json.loads(files[tlv_input.INTERFACE_PATH])
        if (old.keys() != new.keys() or {k for k in old if type(old[k]) is not type(new[k]) or old[k] != new[k]}
                != {tlv_input.INPUT_FIELD}):
            raise ValueError
        revision, value = tlv_input.decode_request(interface, after, tlv_input.PRIMARY_CASE)
        if revision != interface["required_revision"] or value != tlv_input.decode_request(interface, before, tlv_input.PRIMARY_CASE)[1]:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise WorkflowArtifactError("CI_REPAIR_PATCH_INVALID") from None
    patch = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--full-index", "HEAD", "--", TARGET).stdout
    if not patch or len(patch) > MAX_FILE_BYTES or _observe(root, expected)[1] != files:
        raise WorkflowArtifactError("CI_REPAIR_PATCH_CHANGED")
    body = dict(schema_version=SCHEMA, artifact_id=f"{run.workflow_run_id}:{step.step_id}:{step.attempt}",
        workflow_run_id=run.workflow_run_id, step_run_id=step.step_id,
        attempt=step.attempt, opencode_run_id=step.platform_run_id, runtime_instance_id=step.runtime_instance_id,
        external_session_id=result_ref["session_id"], user_message_id=result_ref["user_message_id"],
        assistant_message_id=result_ref["message_id"], baseline_revision=baseline["revision"],
        baseline_tree=baseline["tree"], allowed_paths=baseline["allowed_paths"], changed_paths=[TARGET],
        before_sha256=text_digest(before), after_sha256=text_digest(after), before=before, after=after,
        diff_digest=hashlib.sha256(patch).hexdigest(), diff=patch.decode("utf-8"),
        explanation=f"{tlv_input.INPUT_FIELD}: {old[tlv_input.INPUT_FIELD]} → {new[tlv_input.INPUT_FIELD]}")
    artifact = dict(**body, artifact_digest=sha256_digest(body))
    verify_frozen(artifact)
    return artifact


def verify_frozen(artifact):
    if not isinstance(artifact, dict):
        raise WorkflowArtifactError("CI_REPAIR_ARTIFACT_INVALID")
    body = {k: v for k, v in artifact.items() if k != "artifact_digest"}
    try:
        if (body.get("schema_version") != SCHEMA or body.get("allowed_paths") != [TARGET]
                or body.get("changed_paths") != [TARGET] or body.get("before_sha256") != tlv_input.BASELINE_DIGESTS[TARGET]
                or artifact.get("artifact_digest") != sha256_digest(body)
                or any(not isinstance(body.get(field), str) or text_digest(body[field]) != body.get(digest)
                       for field, digest in (("before", "before_sha256"), ("after", "after_sha256"), ("diff", "diff_digest")))):
            raise ValueError
    except ValueError:
        raise WorkflowArtifactError("CI_REPAIR_ARTIFACT_INVALID") from None
    _verify_patch(body["before"].encode("utf-8"), body["diff"].encode("utf-8"), body["after"].encode("utf-8"), body["after_sha256"])


def verify_current(run, producer):
    artifact = producer.artifact_ref
    verify_frozen(artifact)
    baseline = run.repair_baseline
    _, files = _observe(baseline["root_path"], _baseline_details(baseline))
    if (artifact["baseline_revision"] != baseline["revision"] or artifact["baseline_tree"] != baseline["tree"]
            or files[TARGET] != artifact["after"].encode("utf-8")):
        raise WorkflowArtifactError("CI_REPAIR_PATCH_CHANGED")
