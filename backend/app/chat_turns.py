"""Answer conversational questions without treating them as creative edits."""
import os
import re
from uuid import uuid4
from .tools.ark_responses import request_json,configured_model

EDITING_ZH='“精剪版”用词不准确。目前我能调整剧本、台词和镜头规划，再生成视频；还不能对已有视频进行时间线剪辑、裁切或拼接。刚才是在询问是否精简剧本，不是在承诺剪辑成片。这条追问不会修改你的需求，之前的创作取舍仍保留。'
EDITING_EN='“Edited version” was inaccurate. I can revise the script, dialogue and shot plan before generating video. I cannot currently edit, trim or join existing video on a timeline. I meant shortening the script. Your question does not change the brief; the creative choice remains open.'

def editing_question(text):
    return bool(re.search(r'你.{0,12}(?:会|能|可以).{0,6}剪辑|(?:精剪版|剪辑功能|剪辑能力).*[？?]|can you (?:actually )?edit',text,re.I))

def record_reply(state,text,reply):
    state.conversation.extend([{'id':uuid4().hex,'role':'user','text':text},
                               {'id':uuid4().hex,'role':'assistant','kind':'dialogue_reply','text':reply}])

def respond(state,text,language='zh'):
    if editing_question(text):
        record_reply(state,text,EDITING_EN if language=='en' else EDITING_ZH);return True
    if not re.search(r'[？?]|什么意思|为什么|怎么回事|解释一下|能做什么|^(?:what|why|how|can you)\b',text,re.I):return False
    # Polite requests such as “can you make the light warmer?” are edits;
    # questions about the Agent, its explanation or a pending choice are not.
    try:
        if not os.getenv('ARK_API_KEY'):raise ValueError('No dialogue model')
        context=state.creative_spec.enriched_context
        result=request_json(os.environ['ARK_API_KEY'],configured_model(),
            'Route this conversational turn. Return JSON {"intent":"discuss|revise|answer","reply":"a concise direct answer ONLY for discuss"}. A question about your abilities, wording, reasoning or status is discuss, not authorization to change the brief. Polite actionable requests and explicit creative choices are revise/answer. Do not turn a skeptical follow-up into an answer to the pending creative choice. For discuss answer the actual question, preserve pending choices, never claim a tool ran or planning passed. Current capabilities: script/dialogue/shot planning, reference images, video generation and visual QA. No video timeline editor, clip trimming/joining, or automatic multi-clip production. Do not offer unsupported actions. Use Chinese unless ui_language is en.',
            {'ui_language':language,'message':text,'status':state.status,'pending_choice':context.get('skill_clarification'),
             'recent_conversation':[{'role':m['role'],'text':m.get('text','')[:900]} for m in state.conversation[-6:]]},900,timeout=40,stream=True)
        if result.get('intent') in {'revise','answer'}:return False
        if result.get('intent')!='discuss' or not isinstance(result.get('reply'),str) or not 1<=len(result['reply'])<=1500:raise ValueError('Invalid dialogue response')
        reply=result['reply']
    except (ValueError,OSError,KeyError,TypeError):
        reply='暂时没能完成这条对话回复。你的需求和当前待确认的问题都没有改变，可以再发一次这条追问。' if language!='en' else 'I could not complete this reply. Your brief and pending creative choice are unchanged; please resend the question.'
    record_reply(state,text,reply);return True

def recover_editing_question(state):
    """Repair only the observed, unambiguous legacy misroute; retain its trace."""
    if state.status!='skill_preparation_failed' or len(state.conversation)<3:return False
    user=state.conversation[-2];failure=state.conversation[-1]
    if user.get('role')!='user' or not editing_question(user.get('text','')) or failure.get('kind')!='planning_incomplete':return False
    spec=state.creative_spec;context=spec.enriched_context;text=user['text'];suffix='\n\nAdditional user direction: '+text
    answers=context.get('creative_answers',[])
    if not spec.source_prompt.endswith(suffix) or not answers or answers[-1].get('answer')!=text:return False
    question=answers[-1].get('question')
    if not question:return False
    context.setdefault('recovered_dialogue_turns',[]).append({'question':text,'former_failure':dict(failure),'skill_execution':context.get('skill_execution')})
    context.pop('skill_execution',None);context['creative_answers']=answers[:-1]
    spec.source_prompt=spec.source_prompt[:-len(suffix)]
    if spec.goal.endswith(suffix):spec.goal=spec.goal[:-len(suffix)]
    context['skill_clarification']={'question':question,'reason':'当前仍在确认剧本内容与片长的取舍。'}
    state.status='awaiting_skill_clarification';spec.planning_notice='等待对话中的创作取舍，追问不会修改需求。'
    failure.update(kind='dialogue_reply',text=EDITING_EN if context.get('ui_language')=='en' else EDITING_ZH)
    return True
