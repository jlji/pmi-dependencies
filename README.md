# PMI dependencies from XLNet

cf [scratch nootebook](https://colab.research.google.com/drive/1kJdXQpXhNbTqqdLatH_qfJCeuRD_9ggW#scrollTo=vCfdPAT2QNXd) or [minimal working example notebook](https://colab.research.google.com/drive/1VVcYrRLOUizEbvKvD5_zERHJQLqB_gu4)

Comments are there about the fact that these estimates of PMI are non-symmetric, and how that is dealt with.

## Accuracy of XLNet PMI dependencies

### With batches
[pmi-accuracy.py](pmi-accuracy/pmi-accuracy.py) gets pmi dependencies and calculates undirected attachment score, each sentence being a batch input to the XLNetLMHeadModel.

#### Tokenization
Tokenization is a little bit of an issue, since XLNet is trained on text which is tokenized on the subword level (by Google's [sentencepiece](https://github.com/google/sentencepiece)).  The PTB is tokenized already (_not_ at the subword level), and in order to use the gold parses from the PTB, subword tokenization must be ignored (we're not going to get an accuracy score for dependencies at the level of morphology).

What XLNet expects: tokenized input, according to the sentencepiece format, where `▁` (U+2581, "LOWER ONE EIGHTH BLOCK") corresponds to whitespace.

<!-- A hack method: 
- transform plaintext version of PTB sentences (tokens delineated with spaces) into fake sentencepiece tokenized text, that is, prefixing most PTB tokens with a `▁`.
- use the result as input to XLNet.  This results in a good number of words mapped to id=0 (= `<unk>`) when these tokens are fed into `XLNetTokenizer.convert_tokens_to_ids()`, which might be a problem.
 -->
So I did the following:
- Use sentencepiece tokenizer, just use l-to-r linear chain rule decomposition within words to build up PTB tokens from these smaller subword tokens.  Get PMI between spans of subword tokens corresponding to PTB tokens.  

## Running

Minimal setup is something like the following (depending on the version of cuda). This is what I did on the Azure machine:
```bash
conda create -n pmienv python=3.7
conda activate pmienv
conda install numpy pandas tqdm transformers
conda install pytorch torchvision cudatoolkit=9.2 -c pytorch
pip install transformers
```

Then to run: 
```bash
python pmi-accuracy/pmi-accuracy.py > out.txt
```
<!-- 
or more like perhaps 
`nohup python pmi-accuracy/pmi-accuracy.py --n_observations 100 > out100 2> err100 &` 
or use tmux.
-->

### Reporting

The results will be reported in a timestamped folder in the `/results` dir (or other if specified) like:
```
{results_dir}/xlnet-base-cased_{n_observations}_{date}/
| spec.txt
| scores.csv
| mean_scores.csv
| pmi_matrices.npz
| dependencies.tex
| tikz/
```
- `spec.txt` - just saves the CLI arguments
- `scores.csv` - one row per sentence, reporting the sentence length, number of `<unk>`s and uuas with the four different ways of symmetrizing. Just for quick inspection.
- `mean_scores.csv` - mean uuas over all sentences in one line ignoring NaNs
- `pmi_matrices.npz` - an .npz archive of numpy arrays, with the key 'sentence_`i`' for sentence observation number `i`.
- `dependencies.tex` - a template to run to quickly visualize the predictions (which are in the tikz folder) 


### Saving PMI matrices:

With the cli option `--save_matrices`, PMI matrices are saved to a file 'pmi_matrices.npz' in the results dir.  These can be read back in afterward like this:

```python
if CLI_ARGS.save_matrices:
  npzfile = np.load(RESULTS_DIR+'pmi_matrices.npz')
  print(sorted(npzfile.files))
  print(npzfile['sentence_0'])
```

### Output dependencies as tikz:
To look at the dependency graphs predicted with PMI, say, sentence 42, add a line `\input{tikz/42.tikz}`to the dependencies.tex file, and compile.

--------------------------------------------------

### Dec 2019 version: no batches 
File cached as [pmi-accuracy_nobatch.py](pmi-accuracy/old.pmi-accuracy_nobatch.py), gets pmi dependencies and calculates undirected attachment score, without using batches. Run:
```bash
python pmi-accuracy/old.pmi-accuracy_nobatch.py > out.txt
```
To get the accuracy score for each of the first few (default 20) sentences in the file (default `ptb3-wsj-data/ptb3-wsj-test.conllx`).  CLI options:

- `--n_observations`: the number n of sentences to use. It will calcuate UUAS for the first n sentences (default=`20`).
- `--xlnet-spec`: the XLNet pretrained weights to use: specify `xlnet-base-cased` or `xlnet-large-cased`, or a path to pretrained model and config (see below) (default=`xlnet-base-cased`)
- `--connlx-file`: the path to a dependency file (in [CONLL-X format](https://ilk.uvt.nl/~emarsi/download/pubs/14964.pdf), such as generated by stanford CoreNLP's `trees.EnglishGrammaticalStructure`. See (convert_splits_to_depparse.sh)[scripts/convert_splits_to_depparse.sh]). (default=`ptb3-wsj-data/ptb3-wsj-test.conllx`),
- `--results-dir`: the root folder for results to be generated. A run of the script will generate a timestamped subfolder with results within this directory (default=`results/`)

#### Running this on the cluster
To run on computecanada cluster, which has `pytorch-transformers` (an earlier verion) installed in their wheel, we need to change the corresponding import statement.  This is done by setting the additional optional CLI

- `--offline-mode`

All this will do is change that import statement. But you will also need to specify a path to the pretrained model, since it won't be able to grab it from huggingface's [aws server](https://s3.amazonaws.com/models.huggingface.co/).  So, pre download `xlnet-base-cased-config.json` `xlnet-base-cased-pytorch_model.bin`, and `xlnet-base-cased-spiece.model`, and save them like so:

```bash
mkdir XLNet-base
cd XLNet-base
wget -O pytorch_model.bin https://s3.amazonaws.com/models.huggingface.co/bert/xlnet-base-cased-pytorch_model.bin
wget -O spiece.model https://s3.amazonaws.com/models.huggingface.co/bert/xlnet-base-cased-spiece.model
wget -O config.json https://s3.amazonaws.com/models.huggingface.co/bert/xlnet-base-cased-config.json

mkdir XLNet-large
cd XLNet-large
wget -O pytorch_model.bin https://s3.amazonaws.com/models.huggingface.co/bert/xlnet-large-cased-pytorch_model.bin
wget -O spiece.model https://s3.amazonaws.com/models.huggingface.co/bert/xlnet-large-cased-spiece.model
wget -O config.json https://s3.amazonaws.com/models.huggingface.co/bert/xlnet-large-cased-config.json
```

!Watch out. The pytorch_model.bin files... they're large files. Eg, 

```
XLNet-large/
| config.json         699  
| pytorch_model.bin  1,4G  
| spiece.model       798K 
```

Just specify the directory as `--xlnet-spec  XLNet/`

So, to run on the cluster, do, for instance, something like:
`python pmi-accuracy/pmi-accuracy.py --offline-mode --n_observations 200 --results-dir results-cluster/ --xlnet-spec XLNet-base/`

The results will be reported in a timestamped folder such as:
```
{results_dir}/xlnet-base-cased_{n_observations}_{date}/
| cli-args.txt
| scores.csv
| mean_scores.csv
```