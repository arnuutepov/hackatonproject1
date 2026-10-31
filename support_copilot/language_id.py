"""Transparent script + word evidence. Unsupported/ambiguous input abstains, never guesses by script alone."""
import re
import unicodedata

WORDS = {
 'kk': set('сәлем сәлеметсіз қалай қайда неге менің мені маған сіздер сіз ойын ойынға ойыншы керек жоқ бар үшін көмектес көмек беріңіз берші осындай саған'.split()),
 'uz': set("salom yordam menga meni iltimos qanday qayer nima uchun bilan bering yoq faqat qaysi akaunt akkaunt besabab bo‘yicha shikoyat qilaman qarela noto‘g‘ri ishlamayapti kirdim qayta kuzatyapman hodimi oldirdi o‘ldirdi pul to‘lov".split()),
 'ru': set('привет помогите помощь пожалуйста меня мой мне как где почему игрок админ аккаунт прошу снять наказание не могу игра помоги'.split()),
 'uk': set('мене чому гравець мій допоможіть поможіть помгі адмін перевернувся ласка'.split()),
 'en': set('hello help please my how where why stuck account payment player hacked new'.split()),
 'es': set('hola ayuda por favor cuenta pago gracias'.split()),
 'pt': set('ola olá ajuda minha conta pagamento obrigado'.split()),
}
TRANSLIT_RU = set('pomogi pomogite pamagi osk rodni zastreal zastrial zastral razban spasibo'.split())


def language(text):
 t=unicodedata.normalize('NFC',text).casefold().replace('ё','е')
 t=re.sub(r"[ʻʼ’`']",'‘',t).replace('h3lp','help')
 words=set(re.findall(r"[^\W\d_]+(?:‘[^\W\d_]+)*",t))
 scores={lang:len(words & vocab) for lang,vocab in WORDS.items()}
 if scores['uk'] or re.search(r'[їєґ]',t):
  return 'und','recognized_uk_outside_contract'
 if re.search('[ўҳ]',t): return 'uz',None
 # A lone і is also Ukrainian and appears in corrupted Russian ьі. It is not Kazakh proof.
 if re.search('[әңөұүһ]',t) or scores['kk'] or ('і' in t and re.search('[қғ]',t)):
  return 'kk',None
 if scores['uz'] or re.search(r'\b(?:[a-z]+(?:moqda|yapti|berilgan|tashalgan|bushatila|korilar))\b',t):
  return 'uz',None
 if re.search('[қғ]',t):return 'und','language_shared_script_ambiguous'
 if re.search('[а-я]',t):return 'ru',None
 if words & TRANSLIT_RU:return 'ru','language_transliterated_ru'
 ranked=sorted((scores[lang],lang) for lang in ('en','es','pt'))
 if ranked[-1][0] and ranked[-1][0]>ranked[-2][0]:return ranked[-1][1],None
 return 'und','language_unknown_or_mixed'
