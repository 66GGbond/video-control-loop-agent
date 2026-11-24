"""Answer conversational questions without treating them as creative edits."""
import os
import re
import hashlib
import copy
from uuid import uuid4
from .tools.ark_responses import request_json,configured_model

EDITING_ZH='“精剪版”用词不准确。目前我能调整剧本、台词和镜头规划，再生成视频；还不能对已有视频进行时间线剪辑、裁切或拼接。刚才是在询问是否精简剧本，不是在承诺剪辑成片。这条追问不会修改你的需求，之前的创作取舍仍保留。'
EDITING_EN='“Edited version” was inaccurate. I can revise the script, dialogue and shot plan before generating video. I cannot currently edit, trim or join existing video on a timeline. I meant shortening the script. Your question does not change the brief; the creative choice remains open.'

def pending_question(state):
    context=state.creative_spec.enriched_context;question=None;kind=None
    if state.status=='awaiting_skill_clarification':question=context.get('skill_clarification',{}).get('question');kind='skill'
    elif state.creative_spec.character_reference_plan.get('status')=='awaiting_clarification':
        questions=state.creative_spec.character_reference_plan.get('questions',[]);question=questions[0] if questions else None;kind='character'
    elif state.status=='awaiting_context_confirmation':question='Which country or region is this video for?';kind='context'
    return {'id':hashlib.sha256((kind+':'+question).encode()).hexdigest()[:20],'kind':kind,'text':question} if question else None

def decide(state,text,language='zh'):
    """Every composer turn gets the same contextual decision before mutation."""
    context=state.creative_spec.enriched_context;pending=pending_question(state)
    result=request_json(os.environ['ARK_API_KEY'],configured_model(),
        '''You are the conversational controller of a video creation Agent. Interpret EVERY message using the conversation, original brief, accepted decisions, current stage and exact pending question. Do not route by punctuation or keywords. Return JSON {"intent":"discuss","summary":"normalized decision","dialogue_edit_policy":null,"character_reference_policy":null,"stop_after_generation":null,"creative_update":null,"reply":""}. intent must be one of brief|update|answer|discuss|plan|confirm_generation|confirm_quality|retry|workflow. plan means start/retry creative planning using all accepted inputs, NOT add the planning command to the brief, and does not itself authorize media generation. dialogue_edit_policy is null, preserve or condense_preserve_meaning. character_reference_policy is null, auto or skip; skip only when the user explicitly requests skipping character turnaround images. These policy fields may accompany plan or confirm_generation: one user message may both change production settings and authorize an action. Use null (JSON null, never the string "null") for unchanged fields. Preserve accepted decisions already in resolved_requirements. reply is a concise direct answer for discuss, otherwise empty.
READ-ONLY PRIORITY: A question asking whether an existing choice is retained, or explicitly requesting only an answer/explanation without changing or generating anything, MUST use discuss. Answering such a question is not an update. Example: "我之前要求跳过人物三视图，这项还保留吗？只回答，不修改需求或生成视频。" => discuss with a direct reply. Example: "可以把光线改成冷色吗？" => update. Example: "跳过三视图，直接生成视频" => confirm_generation with skip. An answer resolves the pending question and is stored as a decision, NOT appended as a new brief. A skeptical follow-up/explanation/capability question leaves it pending. A polite request may be an update. Read context for short affirmations, corrections, pronouns and mixed answer-plus-revision; summarize all explicit creative changes. Ask only when a missing decision materially changes the requested result. Explicit permission to shorten dialogue while keeping meaning sets condense_preserve_meaning; never infer that permission solely from duration. Source dialogue remains the reference, not mandatory literal output after such permission. Only explicit requests to begin production/QA may confirm those actions; a creative clarification answer alone is NOT generation approval. Initial supplied creative content is brief; a question on an empty project may still be discuss. Retry does not change requirements.
For a request only about what happens after video generation, use intent workflow. Return stop_after_generation: true if the user asks to finish after rendering or skip subsequent QA and repairs, false only if they explicitly ask to resume those steps, otherwise null. This setting applies to the current production, including an in-flight video. If the turn explicitly requests both a workflow change and a separate script, style or storyboard edit, use update and put only that separate edit in creative_update. Otherwise leave creative_update null. A request to generate may also carry this setting and a duration. Capabilities: script/dialogue/shot planning, image references, one generated video clip at most 30s, visual QA. No timeline editor, trimming/joining, automatic multi-clip production. Do not offer unsupported tools or claim actions already ran. Use Chinese unless ui_language is en. Uploaded Skills and user requests together define the creative task.''',
        {'ui_language':language,'message':text,'status':state.status,'pending_question':pending,
         'original_brief':state.creative_spec.source_prompt,'accepted_decisions':context.get('creative_answers',[]),
         'resolved_requirements':context.get('resolved_requirements',{}),'last_error':context.get('planning_error'),
         'conversation':[{'role':m['role'],'text':m.get('text','')} for m in state.conversation[-24:]],
         'uploaded_skill_names':[d['path'] for d in context.get('uploaded_skills',[])]},1400,timeout=45,stream=True)
    intent=result.get('intent')
    if intent not in {'brief','update','answer','discuss','plan','confirm_generation','confirm_quality','retry','workflow'}:raise ValueError('Invalid chat intent')
    # The model selects the action; internal bookkeeping is filled locally.
    stop = result.get('stop_after_generation')
    creative_update = result.get('creative_update')
    if intent == 'update' and isinstance(stop, bool) and not (
        isinstance(creative_update, str) and creative_update.strip()
    ):
        intent = 'workflow'
    if intent=='brief' and state.status!='ready_for_brief':intent='update'
    if state.status=='ready_for_brief' and intent not in {'discuss','brief','workflow'}:intent='brief'
    if intent=='answer' and not pending:intent='update'
    result['intent']=intent
    result['question_id']=pending['id'] if intent=='answer' else ''
    result['summary']=result.get('summary') or text
    result['evidence']=text
    for field,allowed in (('dialogue_edit_policy',{'preserve','condense_preserve_meaning'}),('character_reference_policy',{'auto','skip'})):
        value=result.get(field)
        result[field]=value if isinstance(value,str) and value in allowed and intent!='discuss' and value!=context.get('resolved_requirements',{}).get(field) else None
    result['stop_after_generation'] = stop if isinstance(stop, bool) and intent != 'discuss' else None
    if intent=='discuss' and not result.get('reply'):raise ValueError('Empty model reply')
    return {**result,'pending_question':pending}

def failure_reply(error,language='zh'):
    import json
    if isinstance(error,json.JSONDecodeError):reason='主 Agent 已返回内容，但返回格式异常，系统未能执行。'
    elif isinstance(error,OSError):reason='连接主 Agent 时发生网络或超时错误，系统未执行操作。'
    else:reason='主 Agent 的处理结果未通过系统校验，操作尚未执行。'
    return reason+'消息已保存，无需重新描述需求。' if language!='en' else 'The main Agent request or action validation failed. Your message is saved; no operation was executed.'

def recover_last_command(state):
    if len(state.conversation)<2:return False
    user,failure=state.conversation[-2:]
    if user.get('role')!='user' or failure.get('kind')!='dialogue_reply' or not failure.get('text','').startswith('这条消息暂时未能完成理解'):return False
    provisional=copy.deepcopy(state);provisional.conversation=provisional.conversation[:-2]
    context=state.creative_spec.enriched_context;decision=decide(provisional,user['text'],context.get('ui_language','zh'))
    if decision['intent'] not in {'update','confirm_generation'}:return False
    requirements=context.setdefault('resolved_requirements',{})
    for field in ('dialogue_edit_policy','character_reference_policy'):
        if decision.get(field):requirements[field]=decision[field]
    if decision['intent']=='confirm_generation':requirements['generation_requested']=True
    if requirements.get('character_reference_policy')=='skip':
        from .character_references import assess
        assess(state)
    context.setdefault('recovered_chat_commands',[]).append({'original_reply':dict(failure),'decision':decision})
    failure.update(kind='model_action_recovered',text='已记录：'+decision['summary'].rstrip('。.!！')+'。当前仍需完成创作规划，后续会按这些选择准备视频，无需重发指令。')
    state.history.append({'phase':'conversation','event':'rejected_command_recovered','payload':decision})
    return True

def reconcile_last_answer(state):
    """Normalize the already-saved answer without resubmitting any generation."""
    if state.status!='skill_preparation_failed':return False
    spec=state.creative_spec;context=spec.enriched_context;answers=context.get('creative_answers',[])
    if not answers or not isinstance(answers[-1],dict):return False
    answer=answers[-1];text=answer.get('answer','');question=answer.get('question','')
    if len(state.conversation)<2 or state.conversation[-2].get('text')!=text or not question:return False
    provisional=copy.deepcopy(state);provisional.status='awaiting_skill_clarification'
    provisional.creative_spec.enriched_context['skill_clarification']={'question':question}
    decision=decide(provisional,text,context.get('ui_language','zh'))
    if decision['intent']!='answer':return False
    answer.update(summary=decision['summary'],question_id=decision['question_id'])
    if decision.get('dialogue_edit_policy'):context.setdefault('resolved_requirements',{})['dialogue_edit_policy']=decision['dialogue_edit_policy']
    suffix='\n\nAdditional user direction: '+text
    if spec.source_prompt.endswith(suffix):
        context.setdefault('source_reconciliations',[]).append({'former_source_prompt':spec.source_prompt,'decision':decision})
        spec.source_prompt=spec.source_prompt[:-len(suffix)]
        if spec.goal.endswith(suffix):spec.goal=spec.goal[:-len(suffix)]
    error=(context.get('skill_execution') or {}).get('error_type')
    if error=='JSONDecodeError':
        spec.planning_notice='你的回答已保存；模型返回的结构化结果格式异常，尚未生成规划。'
        context.setdefault('planning_error',{})['message']=spec.planning_notice
        state.conversation[-1].update(kind='dialogue_reply',text='已记录你的选择：'+decision['summary'].rstrip('。.!！')+'。这次停住是模型返回的结构化结果格式异常，不是需要你重新回答。')
    state.history.append({'phase':'conversation','event':'saved_answer_reconciled','payload':decision})
    return True

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
