"""Pure routing for complete teaching-help requests, without provider dependencies."""
import re


def is_silent_expression_candidate(text, mode='express', correction_context=None):
    """Structural gate only; the model must judge grammar and untranslated pinyin."""
    return (mode == 'express' and correction_context is None and isinstance(text, str)
            and bool(re.search(r'[A-Za-z]', text))
            and not re.search(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]', text))


def is_unchanged_english_expression(text, english, mode='express', correction_context=None):
    """Omit an unchanged English demonstration; this is not proficiency scoring."""
    if not is_silent_expression_candidate(text, mode, correction_context):
        return False
    def normalized(value):
        return re.sub(r'\s+', ' ', value.strip()).rstrip('.').casefold()
    return bool(english.strip()) and normalized(text) == normalized(english)


MEANING_RULES = '''
When modeling, keep who does what to whom, all actions/purposes, negation, stance and final correction. Demonstrating how to make/do is not merely showing. No invented obligation, permission, refusal or question; omit only accidental repeats.
Keep explicit addressees (kinship/names); distinguish address from third-person mention, never invent one or act as the parent. Direct wording help takes precedence: English target ONLY, no outside addressee or bilingual gloss.
When modeling an expression, keep actorless short activities as a neutral English fragment, never invent "Let's"/"I want". Natural paraphrases must retain specific actions: carried is not merely moved; whispered is not merely spoke. Rephrase idiomatically, not word by word. ASR punctuation alone cannot establish an invitation. Coherent pronouns/ellipsis may stay underspecified; do not replace uncertain names/homophones.
Interpret idioms as whole phrases in context. ASR may return Chinese as joined/spaced pinyin, with/without tones: infer only clear phrases; never guess uncertain names/homophones. Keep correct English clauses and their permission/stance; add no purpose or benefit.
A clause missing its predicate/complement MUST clarify, never dangling English or "...". Ask one specific Chinese question, <=20 Chinese characters, no alternatives/explanation. Infer only from relevant context.'''

MEANING_EXAMPLES = '''
Meaning examples, not word-matching rules:
拍球呀。 -> {"intent":"MODE","english":"Bouncing a ball."}
一起跳绳吧。 -> {"intent":"MODE","english":"Let's jump rope."}
奶奶，我找到我的铅笔了。 -> {"intent":"MODE","english":"Grandma, I found my pencil."}
奶奶，橡皮用英语怎么说？ -> {"intent":"MODE","english":"An eraser."}
No rush, màn màn lái. -> {"intent":"MODE","english":"No rush. Take your time."}
这件衣服和那件衣服是不是 -> {"intent":"clarify","english":"你想比较衣服的什么？"}
这件衣服和那件衣服是不是一样大？ -> {"intent":"MODE","english":"Are these clothes the same size?"}
Clarify other unfinished clauses too; never invent their missing meaning.'''

CHAT_MEANING_EXAMPLES = '''
Chat contrasts: answer ordinary turns, model only the requested wording, clarify unfinished meaning.
这件衣服和那件衣服是不是一样大？ -> {"intent":"chat","english":"I can't see the clothes. You can put them together to check."}
跳绳用英语怎么说？ -> {"intent":"chat","english":"Jumping rope."}
奶奶，橡皮用英语怎么说？ -> {"intent":"chat","english":"An eraser."}
这件衣服和那件衣服是不是 -> {"intent":"clarify","english":"你想问衣服的什么？"}
Do not turn a complete content question into a translation-only reply. One relevant follow-up question is allowed in chat.'''


_SIMPLE_HELP_REQUESTS = {
    '再简单一点', '再简单点', '简单一点', '简单点', '说简单一点', '说简单点',
    '用更简单的话说', '讲简单一点', '再说简单一点', '再讲简单一点',
}
_SIMPLE_CHINESE_ONLY_REQUESTS = {
    '再简单一点只用中文说', '再简单点只用中文说', '简单一点只用中文说',
    '再简单一点只用中文解释', '再简单点只用中文解释',
}
_MEANING_REQUESTS = {'什么意思', '这是什么意思', '我没听懂', '我不明白',
                     '用中文解释', '用中文解释一下', '解释一下意思'}
_CHINESE_ONLY_REQUESTS = {'只用中文解释', '只用中文解释一下', '只用中文说',
                         '不要再读原句只解释意思', '只解释意思不要再读原句'}


def direct_help_request(text, previous_english=None, help_context=None):
    """Return (intent, source) only for complete, unquoted help requests."""
    if not isinstance(text, str):
        return None
    # Preserve quotation marks so reporting/quoting a command cannot activate it.
    normalized = re.sub(r'[\s，,。.!！?？;；:：]+', '', text)
    has_help = (isinstance(help_context, dict) and
                isinstance(help_context.get('replay_previous'), bool) and
                all(isinstance(help_context.get(key), str) and help_context[key].strip()
                    for key in ('child', 'assistant')))
    has_previous = isinstance(previous_english, str) and bool(previous_english.strip())
    if normalized in _SIMPLE_HELP_REQUESTS | _SIMPLE_CHINESE_ONLY_REQUESTS:
        if not has_help:
            return None
        source = 'help_context'
    elif normalized in _MEANING_REQUESTS | _CHINESE_ONLY_REQUESTS:
        if not has_help and not has_previous:
            return None
        source = 'help_context' if has_help else 'previous_english'
    else:
        return None
    only = normalized in _SIMPLE_CHINESE_ONLY_REQUESTS | _CHINESE_ONLY_REQUESTS
    replay = help_context['replay_previous'] if source == 'help_context' else True
    return ('explain' if replay and not only and has_previous else 'explain_only', source)
