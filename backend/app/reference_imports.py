"""Bounded user-provided Skill documents; interpreted by the planner, never executed."""
from pathlib import PurePosixPath


def skill_files(value):
    if not isinstance(value,list) or len(value)>50:raise ValueError('Skill 文件最多50个。')
    result=[];total=0
    for item in value:
        if not isinstance(item,dict):raise ValueError('Skill 文件格式无效。')
        path=item.get('path','');text=item.get('text','')
        if not isinstance(path,str) or not path or len(path)>240 or '\\' in path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
            raise ValueError('Skill 文件路径无效。')
        if PurePosixPath(path).suffix.lower() not in {'.md','.txt','.json','.yaml','.yml'} or not isinstance(text,str) or len(text)>32000 or '\x00' in text:
            raise ValueError('仅支持不超过32000字符的文本 Skill 文件。')
        total+=len(text)
        if total>96000:raise ValueError('Skill 文本总量不能超过96000字符。')
        result.append({'path':path,'text':text})
    return result
