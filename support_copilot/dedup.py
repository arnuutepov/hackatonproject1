"""Deterministic 64-bit Simhash candidates. Never merges tickets or suppresses safety."""
import hashlib
import re
import unicodedata


def normalized(text):
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC',text).casefold()))


def simhash(text):
    words=normalized(text).split()
    features=[' '.join(words[i:i+3]) for i in range(max(0,len(words)-2))] or words
    votes=[0]*64
    for feature in set(features):
        value=int.from_bytes(hashlib.blake2b(feature.encode(),digest_size=8).digest(),'big')
        for bit in range(64):votes[bit]+=1 if value & (1<<bit) else -1
    return sum(1<<bit for bit,vote in enumerate(votes) if vote>0)


def near_duplicate(left,right):
    a,b=normalized(left),normalized(right)
    # Short boilerplate is not evidence that two independent players have one case.
    if min(len(a),len(b))<40:return False
    if a==b:return True
    wa,wb=set(a.split()),set(b.split())
    ratio=min(len(a),len(b))/max(len(a),len(b))
    return ratio>=.85 and len(wa & wb)/max(1,len(wa | wb))>=.85 and (simhash(a)^simhash(b)).bit_count()<=3


def repeated_messages(messages):
    texts=[m.text for m in messages if m.role=='player']
    fingerprints=[]
    for text in texts:
        value=normalized(text)
        if len(value)<40:continue
        words=set(value.split());signature=simhash(value)
        for prior,prior_words,prior_signature in fingerprints:
            if value==prior:return True
            if min(len(value),len(prior))/max(len(value),len(prior))>=.85 and (signature^prior_signature).bit_count()<=3 and len(words & prior_words)/max(1,len(words | prior_words))>=.85:
                return True
        fingerprints.append((value,words,signature))
    return False
