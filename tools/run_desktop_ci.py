"""Run every desktop regression in an isolated Qt process with a hard deadline.

QApplication, QSettings and native Qt callbacks are process-global. A fixture
from a previous module must not change the next module's runtime. A crash,
timeout, assertion or missing test module fails the entire check; none skip.
"""
import os,sys,subprocess,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODULES=(
    'independent_chat','companion_chat','companion_memory','driver_voice_repair',
    'floating_chat','fresh_context','recording_draft','realtime_voice',
    'long_voice_stability','voice_resilience','voice_conversation_controls',
    'daily_plan','super_agent_tools','companion_workbench','agent_identity','workflow_catalog',
    'pymaster','sticker','emoji_harvest','sticker_motion','doubao_upload','walk',
)


def main():
    failed=[]
    for name in MODULES:
        print('\n=== tools.test_'+name+' ===',flush=True)
        with tempfile.TemporaryDirectory(prefix='guga-ci-') as folder:
            env=os.environ.copy();env.update(QT_QPA_PLATFORM='offscreen',PYTHONIOENCODING='utf-8',
                GUGA_SUPER_AGENT='off',GUGA_LINGSHU='off',GUGA_TASKS_DB=str(Path(folder)/'tasks.sqlite3'))
            try:
                result=subprocess.run([sys.executable,'-X','utf8','-m','unittest','tools.test_'+name,'-v'],
                                      cwd=ROOT,env=env,timeout=90)
                if result.returncode:failed.append((name,result.returncode))
            except subprocess.TimeoutExpired:failed.append((name,'timeout'))
    print('\nDESKTOP CI:',str(len(MODULES)-len(failed))+'/'+str(len(MODULES)), 'modules passed',flush=True)
    for name,code in failed:print('FAILED',name,code,flush=True)
    return 1 if failed else 0


if __name__=='__main__':sys.exit(main())
