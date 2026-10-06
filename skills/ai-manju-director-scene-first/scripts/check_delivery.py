"""Clip-total-only Markdown preflight. No provider calls or creative acceptance."""
from pathlib import Path
import argparse
import difflib
import hashlib
import json
import math
import re

HEAD = re.compile(r'^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?Clip[ \t]*(\d+)[ \t]*[｜|][ \t]*镜[^｜|\n]+[｜|][ \t]*(\d+(?:\.\d+)?)[ \t]*秒', re.M | re.I)
SHOT = re.compile(r'^[ \t]*(?:\*\*)?镜([^｜|\n]+)[｜|]', re.M)
TIME_LINE = re.compile(r'^[ \t]*[^\n]*秒[ \t]*[｜|][ \t]*镜[^\n]*', re.M)
FIELDS = ('角色', '场景', '上一Clip状态', '画质氛围', '站位', '动作链', '反应链', '情绪变化', '声音', '画面内容', 'Clip结束状态')
FIELD = re.compile(r'^[ \t]*(?:-[ \t]+)?(?:\*\*)?(' + '|'.join(FIELDS) + r')(?:\*\*)?[：:][ \t]*(?:\*\*)?', re.M)
QUOTE = re.compile(r'“([^”\n]+)”|"([^"\n]+)"|「([^」\n]+)」')
SPEECH = re.compile(r'(?:D\d+(?:[（(][^）)\n]*[）)])?|[^\s`：:。；，（）()]{1,20}[（(][^）)\n]*(?:画内|画外|旁白)[^）)\n]*[）)])[ \t]*[：:]', re.I)
OMISSION = re.compile(r'同上|同前(?:一)?|同Clip\s*\d|沿用上', re.I)
EPS = 0.0001


def compact(text):
    """Keep punctuation: ignore only whitespace introduced by text layout."""
    return re.sub(r'\s+', '', text)


def source_quotes(text):
    """Recognize quoted source lines, including speaker: quote on one line."""
    result = []
    for line in text.splitlines():
        for match in QUOTE.finditer(line):
            prefix, suffix = line[:match.start()].strip(), line[match.end():].strip()
            if not suffix and (not prefix or (prefix.endswith(('：', ':')) and not prefix.startswith('△'))):
                result.append(next(x for x in match.groups() if x is not None))
    return result


def speech_fragments(text):
    """Read marked speech; bare unquoted speech ends at a whitespace boundary."""
    fragments = []
    for marker in SPEECH.finditer(text):
        tail = text[marker.end():].lstrip(' \t')
        quoted = QUOTE.match(tail)
        if quoted:
            value = next(x for x in quoted.groups() if x is not None)
        elif text[:marker.start()].count('`') % 2:
            value = tail.split('`', 1)[0].split('\n', 1)[0]
        else:
            value = re.split(r'\s|`|(?:光|动作同步声|音效)[：:]', tail, maxsplit=1)[0]
        if value:
            fragments.append({'text': value.strip(), 'method': 'explicit_marker'})
    if not fragments:
        fragments = [{'text': next(x for x in m.groups() if x is not None), 'method': 'quote_candidate'} for m in QUOTE.finditer(text)]
    return fragments


def compare_speech(source, fragments):
    if source is None:
        return {'status': 'not_checked', 'reason': '未提供来源稿。'}
    originals = source_quotes(source)
    if not originals:
        return {'status': 'not_checked', 'reason': '来源稿没有可识别的独立引号句或角色冒号引号句；不推断无台词。', 'source_quote_count': 0}
    expected = compact(''.join(originals))
    delivered = compact(''.join(p['text'] for p in fragments))
    matched = expected == delivered
    result = {'status': 'matched' if matched else 'needs_review', 'source_quote_count': len(originals),
              'delivered_fragment_count': len(fragments), 'comparison': '按镜序拼接，保留字词与标点，仅忽略空白',
              'uses_quote_candidates': any(p['method'] == 'quote_candidate' for p in fragments),
              'missing_source_quote_candidates': [s for s in originals if compact(s) not in delivered]}
    if not matched:
        edits = []
        for tag, a, b, c, d in difflib.SequenceMatcher(None, expected, delivered, autojunk=False).get_opcodes():
            if tag != 'equal':
                edits.append({'kind': tag, 'source_offset': a, 'delivered_offset': c,
                              'source_excerpt': expected[a:b][:80], 'delivered_excerpt': delivered[c:d][:80]})
        result['differences'] = edits[:12]
        result['difference_count'] = len(edits)
    return result


def audit(text, source=None, clip_min=4, clip_max=15):
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (clip_min, clip_max)) or not 0 < clip_min <= clip_max:
        raise ValueError('Clip上下限必须是有限正数，且下限不大于上限。')
    errors, warnings, clips, fragments = [], [], [], []
    heads = list(HEAD.finditer(text))
    if not heads:
        errors.append('无法识别Clip标题；此检查器仅支持当前Markdown文本格式。')
    for n, head in enumerate(heads):
        block = text[head.start():heads[n + 1].start() if n + 1 < len(heads) else len(text)]
        cid, total = head[1], float(head[2])
        label = f'Clip{cid}'
        row = {'clip': cid, 'seconds': total, 'shots': []}
        fields = list(FIELD.finditer(block))
        by_name = {}
        for field in fields:
            by_name.setdefault(field[1], []).append(field)
        for name in FIELDS:
            if name not in by_name:
                errors.append(f'{label}缺少固定字段：{name}')
        body_start = by_name.get('画面内容', [None])[0]
        body_end = by_name.get('Clip结束状态', [None])[0]
        if body_start and body_end and body_start.start() >= body_end.start():
            errors.append(f'{label}画面内容与结束状态顺序错误。')
        if body_start:
            for name in FIELDS[:9]:
                if name in by_name and by_name[name][0].start() >= body_start.start():
                    errors.append(f'{label}字段{name}应位于画面内容之前。')
        for index, field in enumerate(fields):
            stop = fields[index + 1].start() if index + 1 < len(fields) else len(block)
            value = block[field.end():stop].strip()
            # Restrict this heuristic to opening fields, not speech/process text.
            if field[1] in FIELDS[:5] and OMISSION.search(value):
                errors.append(f'{label}开场字段{field[1]}含跨Clip省略引用，需要展开实际内容。')
            if field[1] != '画面内容' and (not value or re.fullmatch(r'【\s*】|\[\s*\]|(?:待填|TODO)', value, re.I)):
                errors.append(f'{label}字段{field[1]}未填写。')
        if not math.isfinite(total) or not clip_min - EPS <= total <= clip_max + EPS:
            errors.append(f'{label}时长不在{clip_min:g}—{clip_max:g}秒范围。')
        body = block[body_start.end() if body_start else 0:body_end.start() if body_end else len(block)]
        shots = list(SHOT.finditer(body))
        for line in TIME_LINE.finditer(body):
            errors.append(f'{label}仍有逐镜时间行；本版只填写Clip总时长：{line[0].strip()[:100]}')
        if not shots:
            errors.append(f'{label}没有可识别镜头行，应使用“镜编号｜景别…”格式。')
        for k, shot in enumerate(shots):
            stop = shots[k + 1].start() if k + 1 < len(shots) else len(body)
            shot_body = body[shot.end():stop]
            shot_id = shot[1].strip()
            parts = speech_fragments(shot_body)
            for part in parts:
                part.update(clip=cid, shot=shot_id)
            fragments.extend(parts)
            row['shots'].append({'id': shot_id, '_speech_candidates': parts})
        clips.append(row)
    # Fall back to bare quotes only when the entire shot script has no markers.
    method = 'explicit_marker' if any(p['method'] == 'explicit_marker' for p in fragments) else 'quote_candidate'
    fragments = [p for p in fragments if p['method'] == method]
    for clip in clips:
        for shot in clip['shots']:
            parts = [p for p in shot.pop('_speech_candidates') if p['method'] == method]
            chars = len(re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', ''.join(p['text'] for p in parts)))
            shot['speech_characters'] = chars
    speech_check = compare_speech(source, fragments)
    if speech_check['status'] == 'needs_review':
        warnings.append('已识别台词词段与来源引号句未能按序逐字对齐；见speech_check差异。可能为漏改字、重说、乱序或文本识别歧义，需回查原句。')
    elif source is not None and speech_check['status'] == 'not_checked':
        warnings.append(speech_check['reason'])
    return {'provider_call': False, 'timing_mode': 'clip_total_only', 'structural_checks_passed': not errors,
            'creative_quality': 'not_determined', 'video_quality': 'not_determined',
            'clip_limits': {'min': clip_min, 'max': clip_max}, 'errors': errors, 'review_warnings': warnings,
            'speech_check': speech_check, 'speech_fragments': fragments, 'clips': clips,
            'limitations': [
                '本版仅核对Clip总时长，不估算逐镜时间或语速；字段和文字一致性不能证明因果、节奏、三链条、摄影、表演、身份、口型或视频接缝成立。',
                '来源只提取独立引号句或角色冒号后的引号句；输出识别D编号或画内/画外/旁白标记，仅整稿没有任何明确标记时才用引号候选。',
                '混用标记对白与普通引号对白的稿件，只比较已识别的明确标记词段；不能保证混合标注稿的漏字、重说或其他额外台词被发现。',
                '无引号且无反引号包裹的标记词段在空白处结束；词段内有空格时应使用引号或反引号，其他排版需人工核对。',
                '台词比较保留标点但忽略空白；即使匹配，也不能证明来源登记完整、说话人正确或动作反应时序正确。',
                '文本识别差异单列待复核，不改变structural_checks_passed；该字段仅代表本地结构核查。']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    parser.add_argument('--source')
    parser.add_argument('--out', required=True)
    parser.add_argument('--clip-min', type=float, default=4)
    parser.add_argument('--clip-max', type=float, default=15)
    args = parser.parse_args()
    try:
        path = Path(args.input)
        source = Path(args.source).read_text('utf-8-sig') if args.source else None
        result = audit(path.read_text('utf-8-sig'), source, args.clip_min, args.clip_max)
        result['input_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        if args.source:
            result['source_sha256'] = hashlib.sha256(Path(args.source).read_bytes()).hexdigest()
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    except (OSError, ValueError) as exc:
        parser.exit(2, f'检查未完成：{exc}\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('clips', 'limitations', 'speech_fragments')}, ensure_ascii=False))
    return 0 if result['structural_checks_passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
