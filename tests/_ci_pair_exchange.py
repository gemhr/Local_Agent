"""TEST_SCOPE 有界stdio交接，只有可见DTO；不注册生产入口。"""
import asyncio,json,os,sys
from pathlib import Path
from core.persistence.database import Database,DatabaseConfig
from core.runtime.agent_loop.quality_contract import canonical_bytes,catalog_digest
from tests.test_ci_pair_pg import assemble_ci_pair_test_runtime

async def main():
    if os.environ.get('CI_PAIR_EXCHANGE_APPROVED')!='1':raise ValueError('TEST_SCOPE_APPROVAL_REQUIRED')
    raw=sys.stdin.buffer.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise ValueError('TEST_SCOPE_INPUT_LIMIT')
    env=json.loads(raw)
    if set(env)!={'payload','digest'} or catalog_digest(env['payload'])!=env['digest']:raise ValueError('TEST_SCOPE_DIGEST')
    payload=env['payload']
    if set(payload)-{'phase','mode','visible_request','pins'} or payload['phase'] not in ('pins','execute'):raise ValueError('TEST_SCOPE_SHAPE')
    if any(k in raw.decode() for k in ('"ground_truth"','"acceptance_criteria"','"required_sources"')):raise ValueError('GT_LEAK')
    url=os.environ['LOCAL_AGENT_TEST_DATABASE_URL']
    if not url.split('/')[-1].endswith('_test') or '@127.0.0.1:55432/' not in url:raise ValueError('ISOLATION_REQUIRED')
    database=Database(DatabaseConfig(url=url,use_null_pool=True))
    try:
        result=await assemble_ci_pair_test_runtime(database,Path(os.environ['CI_PAIR_EXCHANGE_DIR']),payload)
        encoded=canonical_bytes(result)
        if len(encoded)>2*1024*1024:raise ValueError('TEST_SCOPE_OUTPUT_LIMIT')
        sys.stdout.buffer.write(encoded)
    finally:await database.dispose()

if __name__=='__main__':asyncio.run(main())
