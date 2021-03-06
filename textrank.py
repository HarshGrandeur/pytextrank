#!/usr/bin/env python
# encoding: utf-8

from collections import namedtuple
from datasketch import MinHash
import hashlib
import json
import math
import matplotlib.pyplot as plt
import networkx as nx
import re
import statistics
import string
import textblob
import textblob_aptagger as tag

DEBUG = False # True

ParsedGraf = namedtuple('ParsedGraf', 'id, sha1, graf')
WordNode = namedtuple('WordNode', 'word_id, raw, root, pos, keep, idx')
RankedLexeme = namedtuple('RankedLexeme', 'text, rank, ids, pos')
SummarySent = namedtuple('SummarySent', 'dist, idx, text')


######################################################################
## filter the novel text versus quoted text in an email message

PAT_FORWARD = re.compile("\n\-+ Forwarded message \-+\n")
PAT_REPLIED = re.compile("\nOn.*\d+.*\n?wrote\:\n+\>")
PAT_UNSUBSC = re.compile("\n\-+\nTo unsubscribe,.*\nFor additional commands,.*")


def split_grafs (lines):
  """segment the raw text into paragraphs"""

  graf = []

  for line in lines:
    line = line.strip()

    if len(line) < 1:
      if len(graf) > 0:
        yield "\n".join(graf)
        graf = []
    else:
      graf.append(line)

  if len(graf) > 0:
    yield "\n".join(graf)


def filter_quotes (text, is_email=True):
  """filter the quoted text out of a message"""

  global DEBUG
  global PAT_FORWARD, PAT_REPLIED, PAT_UNSUBSC

  if is_email:
    text = filter(lambda x: x in string.printable, text)

    if DEBUG:
      print("text:", text)

    # strip off quoted text in a forward
    m = PAT_FORWARD.split(text, re.M)

    if m and len(m) > 1:
      text = m[0]

    # strip off quoted text in a reply
    m = PAT_REPLIED.split(text, re.M)

    if m and len(m) > 1:
      text = m[0]

    # strip off any trailing unsubscription notice
    m = PAT_UNSUBSC.split(text, re.M)

    if m:
      text = m[0]

  # replace any remaining quoted text with blank lines
  lines = []

  for line in text.split("\n"):
    if line.startswith(">"):
      lines.append("")
    else:
      lines.append(line)

  return list(split_grafs(lines))


######################################################################
## parse and markup text paragraphs for semantic analysis

PAT_PUNCT = re.compile(r'^\W+$')
PAT_SPACE = re.compile(r'\_+$')

POS_KEEPS = ['v', 'n', 'j']
POS_LEMMA = ['v', 'n']
TAGGER = tag.PerceptronTagger()
UNIQ_WORDS = { ".": 0 }


def is_not_word (word):
  return PAT_PUNCT.match(word) or PAT_SPACE.match(word)


def get_word_id (root):
  """lookup/assign a unique identify for each word root"""

  global UNIQ_WORDS

  # in practice, this should use a microservice via some robust
  # distributed cache, e.g., Cassandra, Redis, etc.

  if root not in UNIQ_WORDS:
    UNIQ_WORDS[root] = len(UNIQ_WORDS)

  return UNIQ_WORDS[root]


def parse_graf (doc_id, graf_text, base_idx):
  """CORE ALGORITHM: parse and markup sentences in the given paragraph"""

  global DEBUG
  global POS_KEEPS, POS_LEMMA, TAGGER

  markup = []
  new_base_idx = base_idx

  for sent in textblob.TextBlob(graf_text).sentences:
    graf = []
    digest = hashlib.sha1()

    tagged_sent = TAGGER.tag(str(sent))
    tag_idx = 0
    raw_idx = 0

    if DEBUG:
      print(tagged_sent)

    while tag_idx < len(tagged_sent):
      pos_tag = tagged_sent[tag_idx]
      word = WordNode(word_id=0, raw=pos_tag[0], root=pos_tag[0], pos=pos_tag[1], keep=0, idx=new_base_idx)

      if DEBUG:
        print("IDX", tag_idx, pos_tag)
        print("reg", is_not_word(pos_tag[0]))
        print("   ", raw_idx, len(sent.words), sent.words)
        print(graf)

      if is_not_word(pos_tag[0]) or (pos_tag[1] == "SYM"):
        parsed_raw = pos_tag[0]
        pos_family = '.'
        word = word._replace(pos = pos_family)
      elif raw_idx < len(sent.words):
        parsed_raw = sent.words[raw_idx]
        pos_family = pos_tag[1].lower()[0]
        raw_idx += 1

      word = word._replace(raw = str(parsed_raw))

      if pos_family in POS_LEMMA:
        word = word._replace(root = str(parsed_raw.singularize().lemmatize(pos_family)).lower())
      elif pos_family != '.':
        word = word._replace(root = str(parsed_raw).lower())
      else:
        word = word._replace(root = str(parsed_raw))

      if pos_family in POS_KEEPS:
        word = word._replace(word_id = get_word_id(word.root), keep = 1)

      digest.update(word.root.encode("utf-8"))

      # schema: word_id, raw, root, pos, keep, idx
      if DEBUG:
        print(word)

      graf.append(list(word))

      new_base_idx += 1
      tag_idx += 1

    markup.append(ParsedGraf(id=doc_id, sha1=digest.hexdigest(), graf=graf))

  return markup, new_base_idx


def parse_doc (json_iter):
  """parse one document to prep for TextRank"""

  global DEBUG

  for meta in json_iter:
    base_idx = 0

    for graf_text in filter_quotes(meta["text"], is_email=False):
      if DEBUG:
        print("graf_text:", graf_text)

      grafs, new_base_idx = parse_graf(meta["id"], graf_text, base_idx)
      base_idx = new_base_idx

      for graf in grafs:
        yield graf


######################################################################
## graph analytics

def get_tiles (graf, size=3):
  """generate word pairs for the TextRank graph"""

  keeps = list(filter(lambda w: w.word_id > 0, graf))
  keeps_len = len(keeps)

  for i in iter(range(0, keeps_len - 1)):
    w0 = keeps[i]

    for j in iter(range(i + 1, min(keeps_len, i + 1 + size))):
      w1 = keeps[j]

      if (w1.idx - w0.idx) <= size:
        yield (w0.root, w1.root,)


def build_graph (json_iter):
  """construct the TextRank graph from parsed paragraphs"""

  global DEBUG, WordNode

  graph = nx.DiGraph()

  for meta in json_iter:
    if DEBUG:
      print(meta["graf"])

    for pair in get_tiles(map(WordNode._make, meta["graf"])):
      if DEBUG:
        print(pair)

      for word_id in pair:
        if not graph.has_node(word_id):
          graph.add_node(word_id)

      try:
        graph.edge[pair[0]][pair[1]]["weight"] += 1.0
      except KeyError:
        graph.add_edge(pair[0], pair[1], weight=1.0)

  return graph


def text_rank (path):
  """run the TextRank algorithm"""

  global DEBUG

  graph = build_graph(json_iter(path))
  ranks = nx.pagerank(graph)

  if DEBUG:
    render_ranks(graph, ranks)

  return graph, ranks


def render_ranks (graph, ranks, img_file="graph.png", show_img=None):
  """render the TextRank graph as an image"""

  nx.draw_networkx(graph)

  if img_file:
    plt.savefig(img_file)

  if show_img:
    plt.show()


######################################################################
## collect key phrases

def find_chunk_sub (phrase, np, i):
  """np chunking - sub"""
  for j in iter(range(0, len(np))):
    p = phrase[i + j]

    if p.text != np[j]:
      return None

  return phrase[i:i + len(np)]


def find_chunk (phrase, np):
  """np chunking"""
  for i in iter(range(0, len(phrase))):
    parsed_np = find_chunk_sub(phrase, np, i)

    if parsed_np:
      return parsed_np

def collect_chunks (phrase):
  """collect the noun phrases"""

  if (len(phrase) > 1):
    found = False
    text = " ".join([rl.text for rl in phrase])

    for np in textblob.TextBlob(text).noun_phrases:
      if np != text:
        found = True
        yield np, find_chunk(phrase, np.split(" "))

    if not found and all([rl.pos[0] != "v" for rl in phrase]):
      yield text, phrase


def normalize_key_phrases (path, ranks):
  """iterator for the normalized key phrases"""

  # first, collect all the single-word keywords
  lex = {}
  max_single_rank = 0.0

  for meta in json_iter(path):
    sent = [w for w in map(WordNode._make, meta["graf"])]
    sent_text = " ".join([w.raw for w in sent])

    if DEBUG:
      print(sent_text)

    for w in sent:
      if (w.word_id > 0) and (w.root in ranks) and (w.pos[0] in "NV"):
        rl = RankedLexeme(text=w.raw.lower(), rank=ranks[w.root], ids=[w.word_id], pos=w.pos.lower())

        if DEBUG:
          print(rl)

        lex[str(rl.ids)] = rl
        max_single_rank = max(max_single_rank, rl.rank)

    # then collect the noun phrases
    tail = 0
    last_idx = sent[0].idx - 1
    phrase = []

    while tail < len(sent):
      w = sent[tail]

      if (w.word_id > 0) and (w.root in ranks) and ((w.idx - last_idx) == 1):
        # keep collecting...
        rl = RankedLexeme(text=w.raw.lower(), rank=ranks[w.root], ids=[w.word_id], pos=w.pos.lower())
        phrase.append(rl)
      else:
        # just hit a phrase boundary
        for text, p in collect_chunks(phrase):
          ids = list(set.union(*[set(rl.ids) for rl in p]))
          rank = math.sqrt(sum([rl.rank**2.0 for rl in p]))/float(len(p)) + max_single_rank
          np_rl = RankedLexeme(text=text, rank=rank, ids=ids, pos="np")

          if DEBUG:
            print(np_rl)

          lex[str(np_rl.ids)] = np_rl

        phrase = []

      last_idx = w.idx
      tail += 1

  sum_ranks = sum(rl.rank for rl in lex.values())

  for rl in sorted(lex.values(), key=lambda rl: rl.rank, reverse=True):
    yield rl._replace(rank=rl.rank / sum_ranks)


######################################################################
## sentence significance

def mh_digest (data, num_perm=512):
  """create a MinHash digest"""
  m = MinHash(num_perm)

  for d in data:
    m.update(d.encode('utf8'))

  return m


def rank_kernel (path):
  """return a list (matrix-ish) of the key phrases and their ranks"""
  kernel = []

  for meta in json_iter(path):
    rl = RankedLexeme(**meta)
    m = mh_digest(map(lambda x: str(x), rl.ids))
    kernel.append((rl, m,))

  return kernel


def top_sentences (kernel, path):
  """determine distance for each sentence"""
  key_sent = {}
  i = 0

  for meta in json_iter(path):
    graf = meta["graf"]
    tagged_sent = [WordNode._make(x) for x in graf]
    text = " ".join([w.raw for w in tagged_sent])

    m_sent = mh_digest([str(w.word_id) for w in tagged_sent])
    dist = sum([m_sent.jaccard(m) * rl.rank for rl, m in kernel])
    key_sent[text] = (dist, i)
    i += 1

  for text, (dist, i) in sorted(key_sent.items(), key=lambda x: x[1][0], reverse=True):
    yield SummarySent(dist=dist, idx=i, text=text)


######################################################################
## document summarization

def limit_keyphrases (path, phrase_limit=20):
  """iterator for the most significant key phrases"""
  rank_thresh = None
  lex = []

  for meta in json_iter(path):
    rl = RankedLexeme(**meta)
    lex.append(rl)

  rank_thresh = statistics.mean([rl.rank for rl in lex])
  used = 0

  for rl in lex:
    if rl.pos[0] != "v":
      if (used > phrase_limit) or (rl.rank < rank_thresh):
        return

      used += 1
      yield rl.text


def limit_sentences (path, word_limit=100):
  """iterator for the most significant sentences, up to a word limit"""
  word_count = 0

  for meta in json_iter(path):
    p = SummarySent(**meta)
    sent_text = p.text.split(" ")
    sent_len = len(sent_text)

    if (word_count + sent_len) > word_limit:
      break
    else:
      word_count += sent_len
      yield sent_text, p.idx


def make_sentence (sent_text):
  """construct a sentence text, with proper spacing"""
  lex = []
  idx = 0

  for word in sent_text:
    if (idx > 0) and not (word[0] in ",.:;!?-\"'"):
      lex.append(" ")

    lex.append(word)
    idx += 1

  return "".join(lex)


######################################################################
## common utilities

def json_iter (path):
  """iterator for JSON-per-line in a file"""

  with open(path, 'r') as f:
    for line in f.readlines():
      yield json.loads(line)


def pretty_print (obj, indent=False):
  """pretty print a JSON object"""

  if indent:
    return json.dumps(obj, sort_keys=True, indent=2, separators=(',', ': '))
  else:
    return json.dumps(obj, sort_keys=True)
