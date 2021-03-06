import os
from tqdm import tqdm
import numpy as np
import pandas as pd
import csv
import shutil
from datetime import datetime
from argparse import ArgumentParser
from collections import namedtuple

import torch

import task
import parser
import languagemodel

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
    observation_class: namedtuple for observations

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

# Running and reporting
def score_observation(model_type, observation, paddings=([], []), saved_matrix=None, verbose=False):
  '''
  gets the unlabeled undirected attachment score for a given sentence (observation),
  by reading off the minimum spanning tree from a matrix of PTB dependency distances
  and comparing that to the maximum spanning tree from a matrix of PMIs from the model
  padding may be added, as tuple of lists of observations for pre- and post-padding resp.
  to use an already computed pmi matrix in saved_matrix, model_type must be 'load_from_disk'
  returns: list_of_scores (list of floats)
  '''
  if verbose:
    obs_df = pd.DataFrame(observation).T
    obs_df.columns = FIELDNAMES
    print("\nGold observation\n", obs_df.loc[:, ['index', 'sentence', 'head_indices']], sep='')

  if model_type == 'load_from_disk':
    pmi_matrix = torch.tensor(saved_matrix)
  elif model_type == 'xlnet':
    # Calculate PMI edges from XLNet
    prepad_tokenlist = [i for x in [obs.sentence for obs in paddings[0]] for i in x]
    postpad_tokenlist = [i for x in [obs.sentence for obs in paddings[1]] for i in x]
    pmi_matrix = MODEL.ptb_tokenlist_to_pmi_matrix(
      observation.sentence, paddings=(prepad_tokenlist, postpad_tokenlist), verbose=verbose)
  elif model_type == 'bert':
    pmi_matrix = MODEL.ptb_tokenlist_to_pmi_matrix(observation.sentence, verbose=verbose)
  else:
    raise ValueError(f'Model type {model_type} not recognized.')

  # Get gold edges distances tensor from conllx file (note 'mst' will always give projective gold edges)
  gold_dist_matrix = task.ParseDistanceTask.labels(observation)
  gold_edges = parser.DepParse('mst', gold_dist_matrix, observation.sentence).tree(symmetrize_method='none',
                                                                                   maximum_spanning_tree=False)
  # Make linear-order baseline distances tensor
  linear_dist_matrix = task.LinearBaselineTask.labels(observation)
  linear_edges = parser.DepParse('mst', linear_dist_matrix, observation.sentence).tree(symmetrize_method='none',
                                                                                   maximum_spanning_tree=False)
  # Instantiate a DepParse object, with the parsetype 'mst', to get pmi mst parse
  mstparser = parser.DepParse('mst', pmi_matrix, observation.sentence)
  pmi_edges = {}
  symmetrize_methods = ['sum', 'triu', 'tril', 'none']
  for symmetrize_method in symmetrize_methods:
    pmi_edges[symmetrize_method] = mstparser.tree(symmetrize_method=symmetrize_method)

  # Instantiate a DepParse object, with parsetype 'projective', to get pmi projective parse
  projparser = parser.DepParse('projective', pmi_matrix, observation.sentence)
  pmi_edges_proj = {}
  for symmetrize_method in symmetrize_methods:
    # note, with Eisner's, symmetrize_method='none' basically gets a directed parse
    pmi_edges_proj[symmetrize_method] = projparser.tree(symmetrize_method=symmetrize_method)

  num_gold = len(gold_edges)
  gold_edges_set = {tuple(sorted(x)) for x in gold_edges}
  print(f'gold set: {sorted(gold_edges_set)}\n')
  linear_edges_set = {tuple(sorted(x)) for x in linear_edges}
  print(f'  linear baseline set: {sorted(linear_edges_set)}')
  common = gold_edges_set.intersection(linear_edges_set)
  linear_baseline_score = len(common)/float(num_gold) if num_gold != 0 else np.NaN
  print(f'  linear_baseline_uuas = {linear_baseline_score:.3f}\n')


  scores = []
  scores_proj = []
  for symmetrize_method in symmetrize_methods:
    pmi_edges_set = {tuple(sorted(x)) for x in pmi_edges[symmetrize_method]}
    pmi_edges_proj_set = {tuple(sorted(x)) for x in pmi_edges_proj[symmetrize_method]}
    print(f'  pmi_edges[{symmetrize_method}]: {sorted(pmi_edges_set)}')
    print(f'  pmi_edges_proj[{symmetrize_method}]: {sorted(pmi_edges_proj_set)}')
    correct = gold_edges_set.intersection(pmi_edges_set)
    correct_proj = gold_edges_set.intersection(pmi_edges_proj_set)
    print("  correct: ", correct)
    print("  correct_proj: ", correct_proj)
    num_correct = len(correct)
    num_correct_proj = len(correct_proj)
    uuas = num_correct/float(num_gold) if num_gold != 0 else np.NaN
    uuas_proj = num_correct_proj/float(num_gold) if num_gold != 0 else np.NaN
    scores.append(uuas)
    scores_proj.append(uuas_proj)
    print(f'  uuas = {num_correct}/{num_gold} = {uuas:.3f}')
    print(f'  proj = {num_correct_proj}/{num_gold} = {uuas_proj:.3f}\n')

  return pmi_matrix, scores, scores_proj, gold_edges, pmi_edges, pmi_edges_proj, linear_baseline_score

def print_tikz(tikz_filepath, predicted_edges, gold_edges, observation, label1='', label2=''):
  ''' Writes out a tikz dependency TeX file for comparing predicted_edges and gold_edges'''
  words = observation.sentence
  gold_edges_set = {tuple(sorted(x)) for x in gold_edges}

  gold_edge_label = {key : None for key in gold_edges_set}
  for i,_ in enumerate(observation.index):
    d,h = int(observation.index[i]), int(observation.head_indices[i])
    if (d-1,h-1) in gold_edges_set:
      gold_edge_label[(d-1,h-1)] = observation.governance_relations[i]
    elif (h-1,d-1) in gold_edges_set:
      gold_edge_label[(h-1,d-1)] = observation.governance_relations[i]

  predicted_edges_set = {tuple(sorted(x)) for x in predicted_edges}
  correct_edges = list(gold_edges_set.intersection(predicted_edges_set))
  incorrect_edges = list(predicted_edges_set.difference(gold_edges_set))
  num_correct = len(correct_edges)
  num_total = len(gold_edges)
  uuas = num_correct/float(num_total) if num_total != 0 else np.NaN
  # replace non-TeXsafe characters... add as needed
  tex_replace = { '$':'\$', '&':'+', '%':'\%', '~':'\textasciitilde', '#':'\#'}
  with open(tikz_filepath, 'a') as fout:
    string = "\\begin{dependency}\n\\begin{deptext}\n"
    string += "\\& ".join([tex_replace[x] if x in tex_replace else x for x in words]) + " \\\\" + '\n'
    string += "\\end{deptext}" + '\n'
    for i_index, j_index in gold_edge_label:
      string += f'\\depedge{{{i_index+1}}}{{{j_index+1}}}{{{gold_edge_label[(i_index, j_index)]}}}\n'
    for i_index, j_index in correct_edges:
      string += f'\\depedge[hide label, edge below, edge style={{blue, opacity=0.5}}]{{{i_index+1}}}{{{j_index+1}}}{{}}\n'
    for i_index, j_index in incorrect_edges:
      string += f'\\depedge[hide label, edge below, edge style={{red, opacity=0.5}}]{{{i_index+1}}}{{{j_index+1}}}{{}}\n'
    string += "\\node (R) at (\\matrixref.east) {{}};\n"
    string += f"\\node (R1) [right of = R] {{\\begin{{footnotesize}}{label1}\\end{{footnotesize}}}};"
    string += f"\\node (R2) at (R1.north) {{\\begin{{footnotesize}}{label2}\\end{{footnotesize}}}};"
    string += f"\\node (R3) at (R1.south) {{\\begin{{footnotesize}}{uuas:.2f}\\end{{footnotesize}}}};"
    string += f"\\end{{dependency}}\n"
    fout.write('\n\n')
    fout.write(string)

def get_padding(i, observations):
  '''
  gets adjacent observations from PTB to add as padding,
  so total length is at least LONG_ENOUGH ptb_tokens long
  input: index and observations
  returns:
    prepadding_observations: list of observations
    postpadding_observations: list of observations
  '''
  j = i
  k = i
  pad_index_set = set()
  total_len = len(observations[i][0])
  # to avoid short sentences on which XLNet performs badly at prediction
  while total_len < LONG_ENOUGH:
    if j - 1 >= 0 and j - 1 not in pad_index_set:
      j -= 1
      pad_index_set.add(j)
      total_len += len(observations[j][0])
    if total_len >= LONG_ENOUGH:
      break
    if k + 1 < len(observations) and k + 1 not in pad_index_set:
      k += 1
      pad_index_set.add(k)
      total_len += len(observations[k][0])
    else: raise ValueError(f'Not enough context to pad up to size {LONG_ENOUGH}!')
  if pad_index_set != set():
    print(f'Using sentence(s) {sorted(pad_index_set)} as padding for sentence {i}.')
  prepadding_observations = [observations[x] for x in sorted(pad_index_set) if x < i]
  postpadding_observations = [observations[x] for x in sorted(pad_index_set) if x > i]
  return prepadding_observations, postpadding_observations

def report_accuracy(model_type, observations, results_dir, device, n_obs='all', save=False, verbose=False):
  '''
  Gets the uuas for observations[0:n_obs], using the specified model type
  Writes to scores and mean_scores csv files.
  Returns:
    number of sentences in total (int)
    list of mean_scores for [sum, triu, tril, none] (ignores NaN values)
  '''
  scores_filepath = os.path.join(results_dir, 'scores.csv')
  all_scores = []
  all_scores_proj = []
  all_linear_baseline_scores = []

  if save:
    save_filepath = os.path.join(results_dir, 'pmi_matrices.npz')
    savez_dict = dict()

  if model_type == 'load_from_disk': # Do not use with save=True
    npzfile = np.load(CLI_ARGS.pmi_from_disk)

  with open(scores_filepath, mode='w') as scores_file:
    scores_writer = csv.writer(scores_file, delimiter=',')
    scores_writer.writerow(['sentence_index', 'sentence_length',
                            'uuas_sum', 'uuas_triu', 'uuas_tril', 'uuas_none',
                            'proj_sum', 'proj_triu', 'proj_tril', 'proj_none',
                            'linear_baseline_score'])
    if n_obs == 'all':
      n_obs = len(observations)

    for i, observation in enumerate(tqdm(observations[:n_obs],
                                         desc=f'{SPEC_STRING} on {device}')):
      if verbose:
        print(f'\n---> observation {i} / {n_obs}')

      paddings = get_padding(i, observations)

      if model_type == 'load_from_disk':
        matrix_i = npzfile[f'sentence_{str(i)}']
        score_returns = score_observation(
          model_type, observation, paddings=paddings, saved_matrix=matrix_i, verbose=verbose)
      else:
        score_returns = score_observation(
          model_type, observation, paddings=paddings, verbose=verbose)
      pmi_matrix, scores, scores_proj, gold_edges, pmi_edges, pmi_edges_proj, linear_baseline_score = score_returns
      scores_writer.writerow([i, len(observation.sentence),
                              scores[0], scores[1], scores[2], scores[3],
                              scores_proj[0], scores_proj[1], scores_proj[2], scores_proj[3],
                              linear_baseline_score])

      if save:
        savez_dict[f'sentence_{i}'] = pmi_matrix

      os.makedirs(os.path.join(results_dir, 'tikz'), exist_ok=True)
      tikz_filepath = os.path.join(results_dir, 'tikz', f'{i}.tikz')
      for symmetrize_method in ['sum', 'triu', 'tril', 'none']:
        '''prints tikz comparing predicted with gold
        for each of the four symmetrize methods in a single file'''
        print_tikz(tikz_filepath, pmi_edges[symmetrize_method],
                   gold_edges, observation,
                   label1=symmetrize_method, label2=i)

      tikz_filepath = os.path.join(results_dir, 'tikz', f'{i}proj.tikz')
      for symmetrize_method in ['sum', 'triu', 'tril', 'none']:
        '''prints tikz comparing predicted with gold
        for each of the four symmetrize methods in a single file'''
        print_tikz(tikz_filepath, pmi_edges_proj[symmetrize_method],
                   gold_edges, observation,
                   label1="proj "+symmetrize_method, label2=i)

      # Just for means
      all_scores.append(scores)
      all_scores_proj.append(scores_proj)
      all_linear_baseline_scores.append(linear_baseline_score)

  shutil.make_archive(os.path.join(results_dir, 'tikz'), 'zip', os.path.join(results_dir, 'tikz'))
  shutil.rmtree(os.path.join(results_dir, 'tikz/'))

  tex_filepath = os.path.join(results_dir, 'dependencies.tex')
  with open(tex_filepath, mode='w') as tex_file:
    tex_file.write("\\documentclass[tikz]{standalone}\n\\usepackage{tikz,tikz-dependency}\n\\pgfkeys{%\n/depgraph/edge unit distance=.75ex,%\n/depgraph/reserved/edge style/.style = {\n-, % arrow properties\nsemithick, solid, line cap=round, % line properties\nrounded corners=2, % make corners round\n},%\n/depgraph/reserved/label style/.style = {%\n% anchor = mid, draw, solid, black, rotate = 0, rounded corners = 2pt,%\nscale = .5,%\ntext height = 1.5ex, text depth = 0.25ex, % needed to center text vertically\ninner sep=.2ex,%\nouter sep = 0pt,%\ntext = black,%\nfill = white, %opacity = 0, text opacity = 0 % uncomment to hide all labels\n},%\n}\n\\begin{document}\n\n% % Put dependency plots here, like\n\\input{tikz/0.tikz}\n\n\\end{document}")


  mean_scores = np.nanmean(np.array(all_scores), axis=0).tolist()
  mean_scores_proj = np.nanmean(np.array(all_scores_proj), axis=0).tolist()
  if verbose:
    print('\n---\nmean_scores:')
    print('nonproj')
    for mzip in zip(['sum', 'triu', 'tril', 'none'], mean_scores):
      print(f'\t{mzip[0]} = {mzip[1]:.3f}')
    print('proj')
    for mzip in zip(['sum_p', 'triu_p', 'tril_p', 'none_p'], mean_scores_proj):
      print(f'\t{mzip[0]} = {mzip[1]:.3f}')
    print(f'\tlinear_baseline = {np.nanmean(all_linear_baseline_scores)}')

  if save:
    np.savez(save_filepath, **savez_dict)

  return n_obs, mean_scores

if __name__ == '__main__':
  ARGP = ArgumentParser()
  ARGP.add_argument('--n_observations', default='all',
                    help='number of sentences to look at')
  ARGP.add_argument('--pmi_from_disk', nargs='?', const='pmi_matrices.npz',
                    help='to use saved matrices from disk (specify path/to/pmi_matrices.npz)') # UNTESTED!
  ARGP.add_argument('--model_spec', default='xlnet-base-cased',
                    help='''specify model, either XLNet ("xlnet-base-cased" or "xlnet-large-cased") 
                            or BERT ("bert-base-cased" or "bert-large-cased"), or path for offline''')
  ARGP.add_argument('--conllx_file', default='ptb3-wsj-data/ptb3-wsj-dev.conllx',
                    help='path/to/treebank.conllx: dependency file, in conllx format')
  ARGP.add_argument('--results_dir', default='results/',
                    help='specify path/to/results/directory/')
  ARGP.add_argument('--save_matrices', action='store_true',
                    help='to save PMI matrices to disk.')
  ARGP.add_argument('--batch_size', default=64, type=int,
                    help='xlnet batch size')
  ARGP.add_argument('--long_enough', default=30, type=int,
                    help='(int) pad sentences to be at least this long')
  CLI_ARGS = ARGP.parse_args()

  SPEC_STRING = str(CLI_ARGS.model_spec)

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

  DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
  print('Using device:', DEVICE)
  if DEVICE.type == 'cuda':
    print(torch.cuda.get_device_name(0))
    print('Memory Usage:')
    print('Allocated:', round(torch.cuda.memory_allocated(0)/1024**3, 1), 'GB')
    print('Cached:   ', round(torch.cuda.memory_cached(0)/1024**3, 1), 'GB')

  # Instantiate the language model to use for getting estimates
  if CLI_ARGS.pmi_from_disk:
    MODEL_TYPE = 'load_from_disk'
  else:
    if CLI_ARGS.model_spec in ['xlnet-base-cased','xlnet-large-cased']:
      MODEL_TYPE = 'xlnet'
      MODEL = languagemodel.XLNet(DEVICE, CLI_ARGS.model_spec, CLI_ARGS.batch_size)
    elif CLI_ARGS.model_spec in ['bert-base-cased','bert-large-cased']:
      MODEL_TYPE = 'bert'
      MODEL = languagemodel.BERT(DEVICE, CLI_ARGS.model_spec, CLI_ARGS.batch_size)
    else:
      raise ValueError(f'Model spec string {CLI_ARGS.model_spec} not recognized.')

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

  LONG_ENOUGH = CLI_ARGS.long_enough
  N_SENTS, MEANS = report_accuracy(MODEL_TYPE, OBSERVATIONS, RESULTS_DIR,
                                   DEVICE, n_obs=N_OBS, 
                                   save=CLI_ARGS.save_matrices, verbose=True)

## Playing around with the output, getting started
# NPZFILEDIR = "results-azure/xlnet-large-cased_2020-02-24-05-46_context60_linbaseline_matrices/pmi_matrices.npz"
# npzfile = np.load(NPZFILEDIR)

# PTBTXTDIR = "ptb3-wsj-data/ptb3-wsj-dev.txt"
# with open(PTBTXTDIR) as f:
# 	sentences = [line.rstrip() for line in f]
