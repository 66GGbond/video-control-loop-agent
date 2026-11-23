"""Legacy planning states and connection error messages (no Skill approval gate)."""
import json
PREPARATION_STATES={'skill_preparation_required','skill_preparation_failed','planning_failed','awaiting_skill_clarification'}

def failure_message(error, language='zh'):
    cause=getattr(error,'__cause__',None)
    if isinstance(error,json.JSONDecodeError) or isinstance(cause,json.JSONDecodeError):
        return ('Your answer is saved. The model returned invalid structured data; planning has not completed. You can retry without repeating your answer.' if language=='en' else '你的回答已保存。模型返回的结构化结果格式异常，规划尚未完成；重试即可继续，不需要重新回答。')
    if isinstance(error, OSError) or isinstance(getattr(error,'__cause__',None),OSError):
        return ('The model connection timed out or was interrupted. Your brief and Skills are saved. Send “retry” to continue.' if language=='en' else '模型连接超时或中断。你的需求和 Skill 已保存，发送“重试”即可继续。')
    return ('Planning could not complete. Your inputs are saved; send “retry” to continue.' if language=='en' else '创作规划暂未完成。你的输入已保存，发送“重试”即可继续。')
