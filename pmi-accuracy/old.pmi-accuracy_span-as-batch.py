"""
Gets the undirected attachment accuracy score
for dependencies calculated from PMI (using XLNet),
compared to gold dependency parses taken from a CONLL file.

Default: run from directory where conllx file
with dependency labels (head_indices column) is located at
ptb3-wsj-data/ptb3-wsj-dev.conllx
-
Jacob Louis Hoover
February 2020
"""

import os
from tqdm import tqdm
import numpy as np
import pandas as pd
import csv
# import matplotlib.pyplot as plt
from datetime import datetime
from argparse import ArgumentParser
from collections import namedtuple#, defaultdict

import torch
import torch.nn.functional as F

import task

def generate_lines_for_sent(lines):
  '''Yields batches of lines describing a sentence in conllx.

  Args:
    lines: Each line of a conllx file.
  Yields:
    a list of lines describing a single sentence in conllx.
  '''
  buf = []
  for line in lines:
    if line.startswith('#'):
      continue
    if not line.strip():
      if buf:
        yield buf
        buf = []
      else:
        continue
    else:
      buf.append(line.strip())
  if buf:
    yield buf

def load_conll_dataset(filepath, observation_class):
  '''Reads in a conllx file; generates Observation objects

  For each sentence in a conllx file, generates a single Observation
  object.

  Args:
    filepath: the filesystem path to the conll dataset

  Returns:
  A list of Observations
  '''
  observations = []
  lines = (x for x in open(filepath))
  for buf in generate_lines_for_sent(lines):
    conllx_lines = []
    for line in buf:
      conllx_lines.append(line.strip().split('\t'))
    # embeddings = [None for x in range(len(conllx_lines))]
    observation = observation_class(*zip(*conllx_lines)
                                    # ,embeddings
                                    )
    observations.append(observation)
  return observations

def get_pmi_matrix_using_spans(ptb_tokenlist, device):
  ''' Input: sentence (string). Returns: an ndArray of PMIs '''
  slength = len(ptb_tokenlist)
  pmis = np.ndarray(shape=(slength, slength))
  for w1_index in tqdm(range(slength)):
    for w2_index in tqdm(range(slength), leave=False,
                         desc=' %i:%s'%(w1_index, ptb_tokenlist[w1_index])):
      if w2_index == w1_index:
        pmis[w1_index][w2_index] = np.NaN # precaution. should not be necessary
      else:
        pmis[w1_index][w2_index] = get_pmi_between_PTBwords(w1_index, w2_index, ptb_tokenlist, 
                                                            device=device, verbose=False)
  return pmis

def word_index_to_subword_indices(word_index, nested_list):
  '''
  Convert from word index for nested list of subword tokens,
  to list of subword token indices
  '''
  if word_index > len(nested_list):
    raise ValueError('word_index exceeds length of nested_list')
  count = 0
  for subword_list in nested_list[:word_index]:
    count += len(subword_list)
  # maybe can do this with functools.reduce
  # count = reduce(lambda x, y: len(x) + len(y),nested_list[:word_index])
  return list(range(count, count+len(nested_list[word_index])))

def make_subword_lists(ptb_tokenlist):
  '''
  Takes list of items from Penn Treebank tokenized text,
  runs the tokenizer to decompose into the subword tokens expected by XLNet.
  Implements some simple custom adjustments to make the results more like what might be expected.
  [TODO: this could be improved, if it is important.  
  For instance, currently it puts an extra space before opening quotes]
  Returns: a list with a sublist for each Treebank item
  '''
  subword_lists = []
  for word in ptb_tokenlist:
    if word == '-LCB-': word = '{'
    elif word == '-RCB-': word = '}'
    elif word == '-LSB-': word = '['
    elif word == '-RSB-': word = ']'
    elif word == '-LRB-': word = '('
    elif word == '-RRB-': word = ')'
    word_tokens = tokenizer.tokenize(word)
    subword_lists.append(word_tokens)
  subword_lists.append(['<sep>'])
  subword_lists.append(['<cls>'])
  # Custom adjustments below
  for i, subword_list_i in enumerate(subword_lists):
    if subword_list_i[0][0] == '▁' and subword_lists[i-1][-1] in ('(','[','{'):
      # print(f'{i}: removing extra space after character. {subword_list_i[0]} => {subword_list_i[0][1:]}')
      subword_list_i[0] = subword_list_i[0][1:]
      if subword_list_i[0] == '': subword_list_i.pop(0)
    if subword_list_i[0] == '▁' and subword_list_i[1] in (')',']','}',',','.','"',"'","!","?") and i != 0:
      # print(f'{i}: removing extra space before character. {subword_list_i} => {subword_list_i[1:]}')
      subword_list_i.pop(0)
    if subword_list_i == ['▁', 'n', "'", 't'] and i != 0:
      # print(f"{i}: fixing X▁n't => Xn 't ")
      del subword_list_i[0]
      del subword_list_i[0]
      subword_lists[i-1][-1]+='n'
  return subword_lists

def get_pmi_between_PTBwords(w1, w2, ptb_tokenlist, device, verbose=False):
  '''
  Input:
    w1,w2: indices of the words in the ptb_tokenlist (ints)
    ptb_tokenlist: PTB-tokenized sentence as list
  returns: PMI(w1;w2|c), where c is the whole sentence except w1,w2
  '''
  subwords_nested = make_subword_lists(ptb_tokenlist)
  w1_indices = word_index_to_subword_indices(w1, subwords_nested)
  w2_indices = word_index_to_subword_indices(w2, subwords_nested)
  flattened_sentence = [tok for sublist in subwords_nested for tok in sublist]
  if verbose:
    print("original sentence", ptb_tokenlist)
    print("input sentence", flattened_sentence)
    print(f"Getting PMI between word {w1} (indices {w1_indices}) and word {w2} (indices {w2_indices}).")
  sentence_as_ids = tokenizer.convert_tokens_to_ids(flattened_sentence)
  pmi = pmi_between_spans(w1_indices, w2_indices, sentence_as_ids, device=device, verbose=False)
  return pmi

def pmi_between_spans(w1_indices, w2_indices, sentence_as_ids, device, verbose=False):
  '''
  Calculates the XLNet PMI estimate betwen the two spans, with a LtoR chain rule assumption within spans.
  Input:
    w1_indices, w2_indices: lists of indices (ints)
    sentence_as_ids: a list of ids (ending with [4,3])
  '''
  seqlen = len(sentence_as_ids)
  len1 = len(w1_indices)
  batch_size = len1*2 #  times 2 for numerator and denominator
  input_ids = torch.tensor(sentence_as_ids).repeat(batch_size, 1)

  perm_mask = torch.zeros((batch_size, seqlen, seqlen), dtype=torch.float)
  target_mapping = torch.zeros((batch_size, 1, seqlen), dtype=torch.float)
  for i, index in enumerate(w1_indices):
    perm_mask[(i, len1+i), :, index:w1_indices[-1]+1] = 1.0   # mask the other w1 tokens to the right
    perm_mask[len1+i, :, w2_indices] = 1.0
    target_mapping[(i, len1+i), :, index] = 1.0 # predict just subtoken i of w1
  if verbose:
    print(f'Batch dimension = {batch_size}')
    print(f'perm_mask[:0]\n{perm_mask[:,0]}')
    print(f'target_mapping\n{target_mapping}')

  with torch.no_grad():
    logits_outputs = model(input_ids.to(device), perm_mask=perm_mask.to(device),
                           target_mapping=target_mapping.to(device)) # gives tuple ([batch_size,1,vocabsize],)
    outputs = F.log_softmax(logits_outputs[0], 2) # log softmax across the vocabulary (dimension 2 of the logits tensor)
    outputs = outputs.view(2, len1, -1) # reshape to be tensor.Size[2,len1,vocabsize]

  outputs = outputs.cpu().numpy()
  log_numerator = sum(outputs[0][i][sentence_as_ids[w1_index]] for i, w1_index in enumerate(w1_indices))
  log_denominator = sum(outputs[1][i][sentence_as_ids[w1_index]] for i, w1_index in enumerate(w1_indices))
  pmi = (log_numerator - log_denominator)
  return pmi


class UnionFind:
  '''
  Naive UnionFind (for computing MST_edges with Prim's algorithm).
  '''
  def __init__(self, n):
    self.parents = list(range(n))
  def union(self, i, j):
    '''
    join i and j
    '''
    if self.find(i) != self.find(j):
      i_parent = self.find(i)
      self.parents[i_parent] = j
  def find(self, i):
    '''
    find parent of i
    '''
    i_parent = i
    while True:
      if i_parent != self.parents[i_parent]:
        i_parent = self.parents[i_parent]
      else:
        break
    return i_parent

def MST_edges(matrix, words, maximum_spanning_tree=True):
  '''
  Constructs a maximum spanning tree using Prim's algorithm.
    (set maximum_spanning_tree=False to get minumum spanning tree instead).
  Input: matrix (ndArray of PMIs), words (list of tokens)
  Excludes edges to/from punctuation symbols or empty strings, and sets np.NaN to -inf
  Returns: tree (list of edges).
  Based on code by John Hewitt.
  '''
  pairs_to_weights = {}
  union_find = UnionFind(len(matrix))
  for i_index, line in enumerate(matrix):
    for j_index, dist in enumerate(line):
      if words[i_index] in ["", "'", "''", ",", ".", ":", "``", "-LRB-", "-RRB-"]:
        continue
      if words[j_index] in ["", "'", "''", ",", ".", ":", "``", "-LRB-", "-RRB-"]:
        continue
      pairs_to_weights[(i_index, j_index)] = dist
  edges = []
  for (i_index, j_index), _ in sorted(pairs_to_weights.items(), 
                                      key=lambda x: float('-inf') if (x[1] != x[1]) else x[1],
                                      reverse=maximum_spanning_tree):
    if union_find.find(i_index) != union_find.find(j_index):
      union_find.union(i_index, j_index)
      edges.append((i_index, j_index))
  return edges

def get_edges_from_matrix(matrix, words, symmetrize_method='sum', verbose=False):
  '''
  Gets Maximum Spanning Tree (list of edges) from pmi matrix, using the specified method.
  input:
    matrix: an array of pmis
    words: a list of tokens
    symmetrize_method:
      'sum' (default): sums matrix with transpose of matrix;
      'triu': uses only the upper triangle of matrix;
      'tril': uses only the lower triangle of matrix;
      'none': uses the optimum weight for each unordered pair of edges.
  returns: tree (list of edges)
  '''

  if symmetrize_method == 'sum':
    matrix = matrix + np.transpose(matrix)
  elif symmetrize_method == 'triu':
    matrix = np.triu(matrix) + np.transpose(np.triu(matrix))
  elif symmetrize_method == 'tril':
    matrix = np.tril(matrix) + np.transpose(np.tril(matrix))
  elif symmetrize_method != 'none':
    raise ValueError("Unknown symmetrize_method. Use 'sum', 'triu', 'tril', or 'none'")

  if verbose:
    print(f'Getting MST from matrix, using symmetrize_method = {symmetrize_method}.')
  edges = MST_edges(matrix, words, maximum_spanning_tree=True)
  return edges

def score_observation(observation, device, verbose=False):
  '''
  gets the unlabeled undirected attachment score for a given sentence (observation),
  by reading off the minimum spanning tree from a matrix of PTB dependency distances
  and comparing that to the maximum spanning tree from a matrix of PMIs
  specify 'cuda' or 'cpu' as device
  returns: list_of_scores (list of floats)
  '''
  if verbose:
    obs_df = pd.DataFrame(observation).T
    obs_df.columns = FIELDNAMES
    print("Gold observation\n", obs_df.loc[:, ['index', 'sentence', 'head_indices']], sep='')

  # Get gold edges from conllx file
  gold_dist_matrix = task.ParseDistanceTask.labels(observation)
  gold_edges = MST_edges(gold_dist_matrix, observation.sentence,
                         maximum_spanning_tree=False)
  ptb_tokenlist = observation.sentence
  print(f"PTB tokenlist, on which tokenizer will be run:\n{ptb_tokenlist}")

  # Calculate pmi edges from XLNet
  pmi_matrix = get_pmi_matrix_using_spans(ptb_tokenlist, device=device)
  if verbose:
    with np.printoptions(precision=2, suppress=True):
      print(f"PMI MATRIX _______ \n{pmi_matrix}\n________")
  pmi_edges = {}
  symmetrize_methods = ['sum', 'triu', 'tril', 'none']
  for symmetrize_method in symmetrize_methods:
    pmi_edges[symmetrize_method] = get_edges_from_matrix(
      pmi_matrix, ptb_tokenlist, symmetrize_method=symmetrize_method, verbose=verbose)

  scores = []
  gold_edges_set = {tuple(sorted(x)) for x in gold_edges}
  print(f'gold set: {sorted(gold_edges_set)}\n')
  for symmetrize_method in symmetrize_methods:
    pmi_edges_set = {tuple(sorted(x)) for x in pmi_edges[symmetrize_method]}
    print(f'pmi_edges[{symmetrize_method:4}]: {sorted(pmi_edges_set)}')
    correct = gold_edges_set.intersection(pmi_edges_set)
    print("correct: ", correct)
    num_correct = len(correct)
    num_total = len(gold_edges)
    uuas = num_correct/float(num_total) if num_total != 0 else np.NaN
    scores.append(uuas)
    print(f'uuas = {num_correct}/{num_total} = {uuas:.3f}\n')
  return scores

def report_uuas_n(observations, results_dir, device, n_obs='all', verbose=False):
  '''
  Draft version.
  Gets the uuas for observations[0:n_obs]
  Writes to scores and mean_scores csv files.
  Returns:
    number of sentences in total (int)
    number of long sentences (int)
    list of mean_scores for [sum, triu, tril, none] (ignores NaN values)
  '''
  results_filepath = os.path.join(results_dir, 'scores.csv')
  all_scores = []
  with open(results_filepath, mode='w') as results_file:
    results_writer = csv.writer(results_file, delimiter=',')
    results_writer.writerow(['sentence_index', 'sentence_length',
                             'uuas_sum', 'uuas_triu', 'uuas_tril', 'uuas_none'])
    if n_obs == 'all':
      n_obs = len(observations)

    for i, observation in enumerate(tqdm(observations[:n_obs])):
      scores = score_observation(observation, device=device, verbose=verbose)
      results_writer.writerow([i, len(observation.sentence),
                               scores[0], scores[1], scores[2], scores[3]])
      all_scores.append(scores)

  mean_scores = np.nanmean(np.array(all_scores), axis=0).tolist()
  if verbose:
    print(f'\n---\nmean_scores[sum,triu,tril,none]: {mean_scores}')
  return n_obs, mean_scores


if __name__ == '__main__':
  ARGP = ArgumentParser()
  ARGP.add_argument('--n_observations', default='all',
                    help='number of sentences to look at')
  ARGP.add_argument('--offline_mode', action='store_true',
                    help='set for "pytorch-transformers" (specify path in xlnet-spec)')
  ARGP.add_argument('--xlnet_spec', default='xlnet-base-cased',
                    help='specify "xlnet-base-cased" or "xlnet-large-cased", or path')
  ARGP.add_argument('--conllx_file', default='ptb3-wsj-data/ptb3-wsj-dev.conllx',
                    help='path/to/treebank.conllx: dependency file, in conllx format')
  ARGP.add_argument('--results_dir', default='results/',
                    help='specify path/to/results/directory/')
  CLI_ARGS = ARGP.parse_args()

  if CLI_ARGS.offline_mode:
    # import pytorch transformers instead of transformers, for use on Compute Canada
    from pytorch_transformers import XLNetLMHeadModel, XLNetTokenizer
    SPEC_STRING = 'offline'
  else:
    from transformers import XLNetLMHeadModel, XLNetTokenizer
    SPEC_STRING = str(CLI_ARGS.xlnet_spec)

  N_OBS = CLI_ARGS.n_observations
  if N_OBS != 'all':
    N_OBS = int(N_OBS)

  NOW = datetime.now()
  DATE_SUFFIX = f'{NOW.year}-{NOW.month:02}-{NOW.day:02}-{NOW.hour:02}-{NOW.minute:02}'
  SPEC_SUFFIX = SPEC_STRING+str(CLI_ARGS.n_observations) if CLI_ARGS.n_observations != 'all' else SPEC_STRING
  RESULTS_DIR = os.path.join(CLI_ARGS.results_dir, SPEC_SUFFIX + '_' + DATE_SUFFIX + '/')
  os.makedirs(RESULTS_DIR, exist_ok=True)
  print(f'RESULTS_DIR: {RESULTS_DIR}\n')

  print('Running pmi-accuracy.py with cli arguments:')
  with open(RESULTS_DIR+'spec.txt', mode='w') as specfile:
    for arg, value in sorted(vars(CLI_ARGS).items()):
      specfile.write(f"\t{arg}:\t{value}\n")
      print(f"\t{arg}:\t{value}")
    specfile.close()

  # Columns of CONLL file
  FIELDNAMES = ['index',
                'sentence',
                'lemma_sentence',
                'upos_sentence',
                'xpos_sentence',
                'morph',
                'head_indices',
                'governance_relations',
                'secondary_relations',
                'extra_info']

  ObservationClass = namedtuple("Observation", FIELDNAMES)
  OBSERVATIONS = load_conll_dataset(CLI_ARGS.conllx_file, ObservationClass)

  DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
  print('Using device:', DEVICE)
  if DEVICE.type == 'cuda':
    print(torch.cuda.get_device_name(0))
    print('Memory Usage:')
    print('Allocated:', round(torch.cuda.memory_allocated(0)/1024**3, 1), 'GB')
    print('Cached:   ', round(torch.cuda.memory_cached(0)/1024**3, 1), 'GB')

  MODEL = [(XLNetLMHeadModel, XLNetTokenizer, CLI_ARGS.xlnet_spec)]
  for model_class, tokenizer_class, pretrained_weights in MODEL:
    tokenizer = tokenizer_class.from_pretrained(pretrained_weights)
    model = model_class.from_pretrained(pretrained_weights)
    model = model.to(DEVICE)

  N_SENTS, MEANS = report_uuas_n(OBSERVATIONS, RESULTS_DIR, 
                                 n_obs=N_OBS, device=DEVICE, verbose=True)
  with open(RESULTS_DIR+'mean_scores.csv', mode='w') as file:
    WRITER = csv.writer(file, delimiter=',')
    WRITER.writerow(['n_sentences',
                     'mean_sum', 'mean_triu', 'mean_tril', 'mean_none'])
    WRITER.writerow([N_SENTS,
                     MEANS[0], MEANS[1], MEANS[2], MEANS[3]])
