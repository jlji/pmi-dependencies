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
from datetime import datetime
from argparse import ArgumentParser
from collections import namedtuple

import torch
import torch.nn.functional as F

import task

# Data input
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

# Tokenization
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

def make_chunks(m, n):
  ''' 
  makes tuples of indices for batching 
  input: 
    m = number of items
    n = size of chunks
  returns: list of indices of form (start,finish) 
  for use in indexing into a list of length m in chunks of size n
  '''
  r = m%n
  d = m//n
  chunked = []
  for i in range(d):
    chunked.append((n*i, n*(i+1)))
  if r != 0:
    chunked.append((d*n, d*n+r))
  return chunked

# Getting a PMI matrix
def ptb_tokenlist_to_pmi_matrix(ptb_tokenlist, device, verbose=False):
  '''
  input: ptb_tokenlist: PTB-tokenized sentence as list
  return: pmi matrix
  '''
  subwords_nested = make_subword_lists(ptb_tokenlist) # note, adds on <cls> and <sep> at the end.
  # indices[i] = list of subtoken indices in subtoken list corresponding to word i in ptb_tokenlist
  indices = [word_index_to_subword_indices(i, subwords_nested) for i, _ in enumerate(subwords_nested[:-2])]

  flattened_sentence = [tok for sublist in subwords_nested for tok in sublist]
  if verbose:
    print(f"PTB tokenlist, on which tokenizer will be run:\n{ptb_tokenlist}")
    print(f"resulting subtokens:\n{flattened_sentence}")
    print(f"correspondence indices:\n{indices}")

  sentence_as_ids = tokenizer.convert_tokens_to_ids(flattened_sentence)
  perm_mask, target_mapping = make_mask_and_mapping(indices)

  # print("check batch dimension is 2*len(indices)*len(flattened(indices))",
  #       target_mapping.size(0) == perm_mask.size(0) ==
  #       2*len(indices)*(len([i for x in indices for i in x])))

  # start and finish indices of BATCH_SIZE sized chunks (0,BATCH_SIZE),(BATCH_SIZE+1,2*BATCH_SIZE), ... 
  index_tuples = make_chunks(perm_mask.size(0), BATCH_SIZE)

  list_of_output_tensors = []
  for index_tuple in tqdm(index_tuples, desc=f'batch size {BATCH_SIZE}', leave=False):
    input_ids = torch.tensor(sentence_as_ids).repeat((index_tuple[1]-index_tuple[0]), 1)
    with torch.no_grad():
      logits_outputs = model(input_ids.to(device),
                             perm_mask=perm_mask[index_tuple[0]:index_tuple[1]].to(device),
                             target_mapping=target_mapping[index_tuple[0]:index_tuple[1]].to(device))
      # note, logits_output is a tuple: ([BATCH_SIZE,1,vocabsize],)
      # log softmax across the vocabulary (dimension 2 of the logits tensor)
      outputs = F.log_softmax(logits_outputs[0], 2)
      list_of_output_tensors.append(outputs)

  outputs_all_batches = torch.cat(list_of_output_tensors).cpu().numpy()
  pmis = get_pmi_matrix_from_outputs(outputs_all_batches, indices, sentence_as_ids)
  return pmis

def get_pmi_matrix_from_outputs(outputs, indices, sentence_as_ids):
  '''
  Gets pmi matrix from the outputs of xlnet
  '''
  # pad[i] the number of items in the batch before the ith word's predictions
  lengths = [len(l) for l in indices]
  cumsum = np.empty(len(lengths)+1, dtype=int)
  np.cumsum(lengths, out=cumsum[1:])
  cumsum[:1] = 0
  pad = list(len(indices)*2*(cumsum))

  pmis = np.ndarray(shape=(len(indices), len(indices)))
  for i, _ in enumerate(indices):
    for j, _ in enumerate(indices):
      start = pad[i]+j*2*len(indices[i])
      end = start+2*len(indices[i])
      span = outputs[start:end]
      pmis[i][j] = get_pmi_from_outputs(span, indices[i], sentence_as_ids)
  return pmis

def get_pmi_from_outputs(outputs, w1_indices, sentence_as_ids):
  '''
  Gets estimated PMI(w1;w2) from XLNet output by collecting and summing over the
  subword predictions, and subtracting the prediction without w2 from the prediction with w2
  '''
  len1 = len(w1_indices)
  outputs = outputs.reshape(2, len1, -1) # reshape to be tensor.Size[2,len1,vocabsize]
  log_numerator = sum(outputs[0][i][sentence_as_ids[w1_index]] for i, w1_index in enumerate(w1_indices))
  log_denominator = sum(outputs[1][i][sentence_as_ids[w1_index]] for i, w1_index in enumerate(w1_indices))
  pmi = (log_numerator - log_denominator)
  return pmi

def make_mask_and_mapping(indices):
  '''
  input:
    indices (list): a list with len(ptb_tokenlist) entries.
      key (int): index in ptb tokenlist
      value (list): indices in subtoken list corresponding to that index
  output: perm_mask, target_mapping
    specification tensors for the whole sentence, for XLNet input;
      - predict each position in each ptb-word once per ptb-word in the sentence
        (except the special characters, need not be predicted)
      - to do this twice (once for numerator once for denominator)
    thus, batchsize of perm_mask and target_mapping will each be
      2 * len(indices) * (seqlen-2)
  '''
  perm_masks = []
  target_mappings = []
  # seqlen = number of items in indices flattened (add two for <sep>, <cls>)
  seqlen = len([i for x in indices for i in x]) + 2
  for i, _ in enumerate(indices):
    for j, _ in enumerate(indices):
      pm_ij, tm_ij = make_mask_and_mapping_single_pair(indices[i], indices[j], seqlen)
      perm_masks.append(pm_ij)
      target_mappings.append(tm_ij)
  perm_mask = torch.cat(perm_masks, 0)
  target_mapping = torch.cat(target_mappings, 0)

  return perm_mask, target_mapping

def make_mask_and_mapping_single_pair(w1_indices, w2_indices, seqlen):
  '''
  Takes two spans of integers (representing the indices of the subtokens
  of two PTB tokens), and returns a permutation mask tensor and an target
  mapping tensor for use in XLNet. The first dimension of the tensors will
  be twice the length of w1_indices.
  input:
    w1_indices,
    w2_indices: lists of indices (ints)
    seqlen = the length of the sentence as subtokens (with special tokens)
  returns:
    perm_mask: a tensor of shape (2*len1, seqlen, seqlen)
    target_mapping: a tensor of shape (2*len1, 1, seqlen)
  '''
  len1 = len(w1_indices)
  batch_size = len1*2 #  times 2 for numerator and denominator

  perm_mask = torch.zeros((batch_size, seqlen, seqlen), dtype=torch.float)
  target_mapping = torch.zeros((batch_size, 1, seqlen), dtype=torch.float)
  for i, index in enumerate(w1_indices):
    perm_mask[(i, len1+i), :, index:w1_indices[-1]+1] = 1.0 # mask the other w1 tokens to the right
    perm_mask[len1+i, :, w2_indices] = 1.0
    target_mapping[(i, len1+i), :, index] = 1.0 # predict just subtoken i of w1
  return perm_mask, target_mapping

# Recovering an MST
class UnionFind:
  '''
  Naive UnionFind (for computing MST with Prim's algorithm).
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

def prims_MST_edges(matrix, words, maximum_spanning_tree=True):
  '''
  Constructs a maximum spanning tree using Prim's algorithm.
    (set maximum_spanning_tree=False to get minumum spanning tree instead).
  Input: matrix (ndArray of PMIs), words (list of tokens)
  Excludes edges to/from punctuation symbols or empty strings, and sets np.NaN to -inf
  Returns: tree (list of edges).
  Based on code by John Hewitt.
  '''
  pairs_to_weights = {}
  excluded = ["", "'", "''", ",", ".", ";", "!", "?", ":", "``", "-LRB-", "-RRB-"]
  union_find = UnionFind(len(matrix))
  for i_index, line in enumerate(matrix):
    for j_index, dist in enumerate(line):
      if words[i_index] in excluded: continue
      if words[j_index] in excluded: continue
      pairs_to_weights[(i_index, j_index)] = dist
  edges = []
  for (i_index, j_index), _ in sorted(pairs_to_weights.items(),
                                      key=lambda x: float('-inf') if (x[1] != x[1]) else x[1],
                                      reverse=maximum_spanning_tree):
    if union_find.find(i_index) != union_find.find(j_index):
      union_find.union(i_index, j_index)
      edges.append((i_index, j_index))
  return edges

def get_MST_from_nonsymmetric_matrix(matrix, words, symmetrize_method='sum'):
  '''
  Gets Maximum Spanning Tree (list of edges) from a nonsymmetric (PMI) matrix,
  using the specified method.
  input:
    matrix: an array of PMIs
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

  edges = prims_MST_edges(matrix, words, maximum_spanning_tree=True)
  return edges

# Running and reporting
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
    print("\nGold observation\n", obs_df.loc[:, ['index', 'sentence', 'head_indices']], sep='')

  # Get gold edges from conllx file
  gold_dist_matrix = task.ParseDistanceTask.labels(observation)
  gold_edges = prims_MST_edges(gold_dist_matrix, observation.sentence, maximum_spanning_tree=False)
  # Make linear-order baseline
  linear_dist_matrix = task.LinearBaselineTask.labels(observation)
  linear_edges = prims_MST_edges(linear_dist_matrix, observation.sentence, maximum_spanning_tree=False)

  # Calculate PMI edges from XLNet
  ptb_tokenlist = observation.sentence
  pmi_matrix = ptb_tokenlist_to_pmi_matrix(ptb_tokenlist, device=device, verbose=verbose)

  pmi_edges = {}
  symmetrize_methods = ['sum', 'triu', 'tril', 'none']
  for symmetrize_method in symmetrize_methods:
    pmi_edges[symmetrize_method] = get_MST_from_nonsymmetric_matrix(
      pmi_matrix, ptb_tokenlist, symmetrize_method=symmetrize_method)

  num_gold = len(gold_edges)
  gold_edges_set = {tuple(sorted(x)) for x in gold_edges}
  print(f'gold set: {sorted(gold_edges_set)}\n')
  linear_edges_set = {tuple(sorted(x)) for x in linear_edges}
  print(f'  linear baseline set: {sorted(linear_edges_set)}')
  common = gold_edges_set.intersection(linear_edges_set)
  linear_baseline_score = len(common)/float(num_gold) if num_gold != 0 else np.NaN
  print(f'  linear_baseline_uuas = {linear_baseline_score:.3f}\n')

  scores = []
  for symmetrize_method in symmetrize_methods:
    pmi_edges_set = {tuple(sorted(x)) for x in pmi_edges[symmetrize_method]}
    print(f'  pmi_edges[{symmetrize_method}]: {sorted(pmi_edges_set)}')
    correct = gold_edges_set.intersection(pmi_edges_set)
    print("  correct: ", correct)
    num_correct = len(correct)
    uuas = num_correct/float(num_gold) if num_gold != 0 else np.NaN
    scores.append(uuas)
    print(f'  uuas = {num_correct}/{num_gold} = {uuas:.3f}\n')
  return pmi_matrix, scores, gold_edges, pmi_edges, linear_baseline_score

def print_tikz(tikz_filepath, predicted_edges, gold_edges, words, label1='', label2=''):
  ''' Writes out a tikz dependency TeX file for comparing predicted_edges and gold_edges'''
  gold_edges_set = {tuple(sorted(x)) for x in gold_edges}
  predicted_edges_set = {tuple(sorted(x)) for x in predicted_edges}
  correct_edges = list(gold_edges_set.intersection(predicted_edges_set))
  incorrect_edges = list(predicted_edges_set.difference(gold_edges_set))
  num_correct = len(correct_edges)
  num_total = len(gold_edges)
  uuas = num_correct/float(num_total) if num_total != 0 else np.NaN
  # replace non-TeXsafe characters... add as needed
  tex_replace = { '$':'\$', '&':'\&', '%':'\%', '~':'\textasciitilde', '#':'\#'}
  with open(tikz_filepath, 'a') as fout:
    string = "\\begin{dependency}[hide label, edge unit distance=.5ex]\n\\begin{deptext}[column sep=0.0cm]\n"
    string += "\\& ".join([tex_replace[x] if x in tex_replace else x for x in words]) + " \\\\" + '\n'
    string += "\\end{deptext}" + '\n'
    for i_index, j_index in gold_edges:
      string += f'\\depedge{{{i_index+1}}}{{{j_index+1}}}{{{"."}}}\n'
    for i_index, j_index in correct_edges:
      string += f'\\depedge[edge style={{blue, opacity=0.5}}, edge below]{{{i_index+1}}}{{{j_index+1}}}{{{"."}}}\n'
    for i_index, j_index in incorrect_edges:
      string += f'\\depedge[edge style={{red, opacity=0.5}}, edge below]{{{i_index+1}}}{{{j_index+1}}}{{{"."}}}\n'
    string += "\\node (R) at (\\matrixref.east) {{}};\n"
    string += f"\\node (R1) [right of = R] {{\\begin{{footnotesize}}{label1}\\end{{footnotesize}}}};"
    string += f"\\node (R2) at (R1.north) {{\\begin{{footnotesize}}{label2}\\end{{footnotesize}}}};"
    string += f"\\node (R3) at (R1.south) {{\\begin{{footnotesize}}{uuas:.2f}\\end{{footnotesize}}}};"
    string += f"\\end{{dependency}}\n"
    fout.write('\n\n')
    fout.write(string)

def report_uuas_n(observations, results_dir, device, n_obs='all', save=False, verbose=False):
  '''
  Gets the uuas for observations[0:n_obs]
  Writes to scores and mean_scores csv files.
  Returns:
    number of sentences in total (int)
    list of mean_scores for [sum, triu, tril, none] (ignores NaN values)
  '''
  scores_filepath = os.path.join(results_dir, 'scores.csv')
  all_scores = []
  all_linear_baseline_scores = []

  if save:
    save_filepath = os.path.join(results_dir, 'pmi_matrices.npz')
    savez_dict = dict()

  with open(scores_filepath, mode='w') as scores_file:
    scores_writer = csv.writer(scores_file, delimiter=',')
    scores_writer.writerow(['sentence_index', 'sentence_length',
                            'uuas_sum', 'uuas_triu', 'uuas_tril', 'uuas_none',
                            'linear_baseline_score'])
    if n_obs == 'all':
      n_obs = len(observations)

    for i, observation in enumerate(tqdm(observations[:n_obs], desc='computing PMIs')):
      
      if verbose:
        print(f'\n---> observation {i} / {n_obs}')
      score_returns = score_observation(observation, device=device, verbose=verbose)
      pmi_matrix, scores, gold_edges, pmi_edges, linear_baseline_score = score_returns
      scores_writer.writerow([i, len(observation.sentence),
                              scores[0], scores[1], scores[2], scores[3],
                              linear_baseline_score])

      if save:
        savez_dict[f'sentence_{i}'] = pmi_matrix

      os.makedirs(os.path.join(results_dir, 'tikz'), exist_ok=True)
      tikz_filepath = os.path.join(results_dir, 'tikz', f'{i}.tikz')
      for symmetrize_method in ['sum', 'triu', 'tril', 'none']:
        '''prints tikz comparing predicted with gold
        for each of the four symmetrize methods in a single file'''
        print_tikz(tikz_filepath, pmi_edges[symmetrize_method],
                   gold_edges, observation.sentence,
                   label1=symmetrize_method, label2=i)

      # Just for means
      all_scores.append(scores)
      all_linear_baseline_scores.append(linear_baseline_score)

  tex_filepath = os.path.join(results_dir, 'dependencies.tex')
  with open(tex_filepath, mode='w') as tex_file:
    tex_file.write("\\documentclass[tikz]{standalone}\n\\usepackage{tikz,tikz-dependency}\n\\pgfkeys{%\n/depgraph/reserved/edge style/.style = {%\n-, % arrow properties\nsemithick, solid, line cap=round, % line properties\nrounded corners=2, % make corners round\n},%\n}\n\\begin{document}\n% % Put dependency plots here, like\n\\input{tikz/0.tikz}\n\\end{document}")

  mean_scores = np.nanmean(np.array(all_scores), axis=0).tolist()
  if verbose:
    print('\n---\nmean_scores:')
    for x in zip(['sum', 'triu', 'tril', 'none'],mean_scores):
      print(f'    {x[0]} = {x[1]}')
    print(f'linear_baseline = {np.nanmean(all_linear_baseline_scores)}')

  if save:
    np.savez(save_filepath, **savez_dict)

  return n_obs, mean_scores

if __name__ == '__main__':
  ARGP = ArgumentParser()
  ARGP.add_argument('--n_observations', default='all',
                    help='number of sentences to look at')
  ARGP.add_argument('--offline_mode', action='store_true',
                    help='to use "pytorch-transformers" (specify path in xlnet-spec)')
  ARGP.add_argument('--xlnet_spec', default='xlnet-base-cased',
                    help='specify "xlnet-base-cased" or "xlnet-large-cased", or path')
  ARGP.add_argument('--conllx_file', default='ptb3-wsj-data/ptb3-wsj-dev.conllx',
                    help='path/to/treebank.conllx: dependency file, in conllx format')
  ARGP.add_argument('--results_dir', default='results/',
                    help='specify path/to/results/directory/')
  ARGP.add_argument('--save_matrices', action='store_true',
                    help='to save PMI matrices to disk.')
  ARGP.add_argument('--batch_size', default=64, type=int,
                    help='xlnet batch size')
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
  BATCH_SIZE = CLI_ARGS.batch_size

  N_SENTS, MEANS = report_uuas_n(OBSERVATIONS, RESULTS_DIR,
                                 n_obs=N_OBS, device=DEVICE,
                                 save=CLI_ARGS.save_matrices, verbose=True)

  # Write mean scores just for sanity check
  with open(RESULTS_DIR+'mean_scores.csv', mode='w') as file:
    WRITER = csv.writer(file, delimiter=',')
    WRITER.writerow(['n_sentences',
                     'mean_sum', 'mean_triu', 'mean_tril', 'mean_none'])
    WRITER.writerow([N_SENTS,
                     MEANS[0], MEANS[1], MEANS[2], MEANS[3]])