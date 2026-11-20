"""Keep stage directions out of speech and map them to actual pet gestures."""
import re

ALIASES = {'摇头':'head_shake','摇摇头':'head_shake','点头':'nod','点点头':'nod',
           '歪头':'tilt','挥手':'wave','挥挥手':'wave','开心蹦':'bounce','蹦跳':'bounce',
           '抖动':'shake','指点':'point','偷看':'peek','傲娇摇头':'head_shake',
           'tsundere':'head_shake','戴墨镜':'sunglasses','摘墨镜':'remove_sunglasses',
           '跳舞':'dance','摇摆':'dance','晃动身体':'dance','停止跳舞':'stop_dance','暗中观察':'sneak_peek'}
ALIASES.update({'抬头回应':'look_up_step','打盹':'idle_doze','困倦打盹':'idle_doze','比心':'idle_heart','双手比心':'idle_heart',
                '嘟嘴':'idle_pout','鼓脸':'idle_pout','生闷气':'idle_pout','哼一声':'idle_pout','抱臂嘟嘴':'idle_pout',
                '大笑':'idle_laugh','仰头笑':'idle_laugh','哈哈笑':'idle_laugh','开怀大笑':'idle_laugh'})
# Single source of truth: any reviewed clip under resources/actions/idle-motion
# becomes triggerable by adding its name here.
IDLE_GESTURES = ('idle_doze', 'idle_heart', 'idle_pout', 'idle_laugh')
GESTURES = {'head_shake','nod','tilt','wave','bounce','shake','point','peek','none','sunglasses','remove_sunglasses','dance','stop_dance','sneak_peek','look_up_step'} | set(IDLE_GESTURES)
GESTURES.add('look_up_step')

def spoken_reply(text):
    text = str(text).strip()
    expression, action = 'normal', 'none'
    parts = [p.strip() for p in text.split('|',2)]
    if len(parts)==3 and (parts[1] in GESTURES or parts[1] in ALIASES):
        expression = parts[0] if parts[0] in {'normal','happy','sleepy','surprised','sad','playful'} else 'normal'
        action = ALIASES.get(parts[1],parts[1]); text = parts[2]
    # Only explicit stage directions; preserve normal statements such as “不要摇头”.
    pattern = r'(?:[（(【\[]\s*|\*\*?\s*)('+'|'.join(sorted(ALIASES,key=len,reverse=True))+r')(?:\s*[）)】\]]|\s*\*\*?)'
    def remove(match):
        nonlocal action
        if action=='none': action=ALIASES[match.group(1)]
        return ''
    text=re.sub(pattern,remove,text)
    return expression,action,text.strip()


# Plain replies and audio transcripts may have no expression metadata.
# Restrict inference to conversational cues, never actions mentioned as topics.
_REACTIONS = (
    (re.compile(r'^(?:别担心|辛苦了|听起来(?:很|挺)?(?:难过|不容易)|抱抱)'), 'sad', 'nod'),
    (re.compile(r'^(?:哇[！!，,]|诶[？?，,]|咦[？?，,]|居然|竟然)'), 'surprised', 'tilt'),
    (re.compile(r'^(?:哈哈[哈]*|嘿嘿|太好了|好耶|恭喜)'), 'happy', 'nod'),
    (re.compile(r'^(?:你好[！!，,～~ ]|(?:早上|晚上|下午)好[！!，,～~ ]|嗨[！!，,～~ ])'), 'happy', 'wave'),
    (re.compile(r'^(?:好(?:的|呀)?[！!，,。～~]|明白了|收到[！!，,。～~])'), 'normal', 'nod'),
)


def reply_performance(text, expression=None, action='none'):
    clean = re.sub(r'^(?:咕嘎[～~！!，, ]+|[*#　 ]+)', '', str(text).strip()).strip()
    for cue, face, gesture in _REACTIONS:
        if cue.search(clean):
            return expression or face, action if action != 'none' else gesture
    return expression, action
