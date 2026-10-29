"""单一 CI Instructions 候选工件；不授予执行、发布或模型调用权限。"""
from dataclasses import replace
import difflib
from pathlib import Path

from core.agent_platform.ci_diagnosis import definition
from core.runtime.agent_loop.quality_contract import canonical_bytes, catalog_digest

SCHEMA = 'ci-prompt-candidate.v1'


def prepare(instructions, *, feedback_refs, review_refs, change_reason, created_at):
    """受控调用方构造优化假设；返回值仍须独立审核，不自动成为执行资产。"""
    base = definition()
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions.encode('utf-8')) > 32768:
        raise ValueError('CI_PROMPT_CONTENT_INVALID')
    if not feedback_refs or not review_refs or not change_reason or not created_at:
        raise ValueError('CI_PROMPT_PROVENANCE_REQUIRED')
    manifest = dict(schema_version=SCHEMA, base_asset_identity=base.agent_id + '@' + base.agent_version,
        base_asset_digest=catalog_digest(base.instructions), candidate_asset_digest=catalog_digest(instructions),
        source_feedback_refs=list(feedback_refs), source_review_refs=list(review_refs),
        change_reason=change_reason, created_at=created_at, source_type='SYNTHETIC',
        claim='OPTIMIZATION_HYPOTHESIS', approval_state='PENDING',
        rollback_reference=catalog_digest(base.instructions),
        content_diff=''.join(difflib.unified_diff(base.instructions.splitlines(True), instructions.splitlines(True),
            fromfile='baseline', tofile='candidate')))
    manifest['candidate_id'] = 'ci-prompt-' + catalog_digest(manifest)
    return dict(manifest=manifest, manifest_digest=catalog_digest(manifest), instructions=instructions)


def validate(asset):
    if set(asset) != {'manifest', 'manifest_digest', 'instructions'}:
        raise ValueError('CI_PROMPT_ASSET_SHAPE_INVALID')
    m = asset['manifest']
    expected = prepare(asset['instructions'], feedback_refs=m['source_feedback_refs'],
        review_refs=m['source_review_refs'], change_reason=m['change_reason'], created_at=m['created_at'])
    if asset != expected:
        raise ValueError('CI_PROMPT_ASSET_PIN_INVALID')
    return asset


def freeze(path, asset):
    """仅供受信运维/离线工件调用；不接入 HTTP，不覆盖任何已存在版本。"""
    validate(asset)
    encoded = canonical_bytes(asset)
    with Path(path).open('xb') as handle:
        handle.write(encoded)


def select(asset, *, approved_manifest_digest, baseline=False):
    """显式审批摘要来自受信配置；不修改 registry、默认定义或全局 Instructions。"""
    validate(asset)
    if approved_manifest_digest != asset['manifest_digest']:
        raise ValueError('CI_PROMPT_NOT_APPROVED')
    base = definition()
    return base if baseline else replace(base, instructions=asset['instructions'])


def request_binding(asset, messages, *, run_id, attempt_id, frozen_input_digest, model_request_digest):
    """请求构造检查器，不冒充持久准入、网络发送或模型消费证明。"""
    validate(asset)
    system = [m.get('content') for m in messages if m.get('role') == 'system']
    content = asset['instructions']
    matches = [s for s in system if isinstance(s, str) and content in s]
    if len(matches) != 1:
        raise ValueError('CI_PROMPT_ACTUAL_INSTRUCTIONS_MISMATCH')
    evidence = dict(schema_version='ci-prompt-request-binding.v1', candidate_id=asset['manifest']['candidate_id'],
        manifest_digest=asset['manifest_digest'], prompt_digest=catalog_digest(content),
        system_message_digest=catalog_digest(matches[0]), messages_digest=catalog_digest(messages),
        model_request_digest=model_request_digest, run_id=run_id, attempt_id=attempt_id,
        frozen_input_digest=frozen_input_digest, evidence_scope='REQUEST_CONSTRUCTION_ONLY')
    return dict(evidence=evidence, receipt_digest=catalog_digest(evidence))


def configured_bundle(bundle, path):
    """仅由 Composition Root 在注册表冻结前调用，配置路径沿既有 Quality 设置。"""
    if not path:
        return bundle
    from core.runtime.agent_loop.quality_contract import parse_quality_json
    raw = Path(path).read_bytes()
    if len(raw) > 128 * 1024:
        raise ValueError('CI_PROMPT_CONFIGURATION_LIMIT')
    config = parse_quality_json(raw)
    if 'ci_prompt' not in config:
        return bundle
    selection = config['ci_prompt']
    if set(selection) != {'asset', 'approved_manifest_digest', 'mode'} or selection['mode'] not in {'BASELINE', 'CANDIDATE'}:
        raise ValueError('CI_PROMPT_CONFIGURATION_INVALID')
    asset = validate(selection['asset'])
    selected = select(asset, approved_manifest_digest=selection['approved_manifest_digest'], baseline=selection['mode'] == 'BASELINE')
    pin = dict(schema_version='ci-prompt-selection.v1', candidate_id=asset['manifest']['candidate_id'],
        manifest_digest=asset['manifest_digest'], mode=selection['mode'],
        prompt_digest=catalog_digest(selected.instructions))
    selected = replace(selected, business_options={**selected.business_options, 'ci_prompt_selection': pin})
    matches = [r for r in bundle.registrations if r.definition.agent_id == selected.agent_id]
    if len(matches) != 1:
        raise ValueError('CI_PROMPT_REGISTRATION_INVALID')
    return replace(bundle, registrations=tuple(replace(r, definition=selected)
        if r.definition.agent_id == selected.agent_id else r for r in bundle.registrations))

