"""Resolve the current utterance language independently of source-book text."""
import re


def unquoted(text):
    text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"', ' ', text)
    return re.sub(r"(?<!\w)'[^'\n]+'(?!\w)", ' ', text)


def text_language(text):
    """Allow short foreign names without changing the surrounding language."""
    body = unquoted(text)
    if not re.search(r'[\u3400-\u9fffA-Za-z]', body):
        body = text
    han = len(re.findall(r'[\u3400-\u9fff]', body))
    latin = len(re.findall(r'[A-Za-z]', body))
    return 'zh' if han and han >= latin * .3 else 'en'


def response_language(question):
    body = unquoted(question)
    if re.search(r'用英[文语]|英[文语]回答|(?:answer|reply|respond)\s+in\s+English', body, re.I):
        return 'en'
    if re.search(r'用中[文语]|中文回答|(?:answer|reply|respond)\s+in\s+Chinese', body, re.I):
        return 'zh'
    return text_language(question)
