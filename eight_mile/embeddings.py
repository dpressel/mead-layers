import io
import copy
import logging
import collections
import contextlib
import numpy as np
from eight_mile.utils import optional_params, exporter, write_json, read_config_file, Offsets, mime_type

__all__ = []
export = exporter(__all__)
logger = logging.getLogger("mead.layers")


@export
def norm_weights(word_vectors):
    norms = np.linalg.norm(word_vectors, axis=1, keepdims=True)
    norms = (norms == 0) + norms
    return word_vectors / norms


@export
def write_word2vec_file(filename, vocab, word_vectors):
    """Write out a binary word2vec file

    This function allows either a list of vocabulary of the same size
    as the vectors, in which case it does a simple write, or a vocabulary
    dict which may contain "magic" Offset values that baseline uses.  If
    those are populated offsets in the dict, they will occupy the low indices
    (first values), so to prune them, remove them from the vocab and shift the
    indices down

    :param filename: (`str`) The filename
    :param vocab: (`Dict[str, int]` or `List[str]`) A list of vocabulary
    :param word_vectors: The word vectors to write for each vocab
    :return: None
    """

    offset = 0
    # Check for case where we have a dict and possibly magic values to prune
    if isinstance(vocab, collections.Mapping):
        vocab_copy = copy.deepcopy(vocab)

        for v in Offsets.VALUES:
            if v in vocab_copy:
                del vocab_copy[v]
                offset += 1
        vocab_list = [0] * len(vocab_copy)
        for word, idx in vocab_copy.items():
            vocab_list[idx - offset] = word
    # Otherwise its just a list dont do anything weird
    else:
        vocab_list = vocab

    # Writing the file is pretty simple, just need the vocab size and depth
    # Then write each line
    with io.open(filename, "wb") as f:
        word_vectors_offset = word_vectors[offset:]
        vsz = len(vocab_list)
        dsz = word_vectors[0].shape[0]
        f.write(bytes("{} {}\n".format(vsz, dsz), encoding="utf-8"))
        assert len(vocab_list) == len(word_vectors_offset)
        for word, vector in zip(vocab_list, word_vectors_offset):
            vec_str = vector.tobytes()
            f.write(bytes("{} ".format(word), encoding="utf-8") + vec_str)


@export
class EmbeddingsModel(object):
    def __init__(self):
        super(EmbeddingsModel, self).__init__()

    def get_dsz(self):
        pass

    def get_vsz(self):
        pass

    def get_vocab(self):
        pass

    def save_md(self, target):
        pass


def pool_vec(embeddings, tokens, operation=np.mean):
    if type(tokens) is str:
        tokens = tokens.split()
    try:
        return operation([embeddings.lookup(t, False) for t in tokens], 0)
    except:
        return embeddings.weights[0]


@export
class WordEmbeddingsModel(EmbeddingsModel):
    def __init__(self, **kwargs):
        super(WordEmbeddingsModel, self).__init__()
        self.vocab = kwargs.get("vocab")
        self.vsz = kwargs.get("vsz")
        self.dsz = kwargs.get("dsz")
        self.weights = kwargs.get("weights")
        if "md_file" in kwargs:
            md = read_config_file(kwargs["md_file"])
            self.vocab = md["vocab"]
            self.vsz = md["vsz"]
            self.dsz = md["dsz"]
        if "weights_file" in kwargs:
            self.weights = np.load(kwargs["weights_file"]).get("arr_0")

        if self.weights is not None:
            if self.vsz is None:
                self.vsz = self.weights.shape[0]
            else:
                assert self.vsz == self.weights.shape[0]
            if self.dsz is None:
                self.dsz = self.weights.shape[1]
            else:
                assert self.dsz == self.weights.shape[1]

        elif self.vsz is not None and self.dsz is not None:
            self.weights = np.zeros((self.vsz, self.dsz))

    def get_dsz(self):
        return self.dsz

    def get_vsz(self):
        return self.vsz

    def get_vocab(self):
        return self.vocab

    def get_weights(self):
        return self.weights

    def save_md(self, target):
        write_json({"vsz": self.get_vsz(), "dsz": self.get_dsz(), "vocab": self.get_vocab()}, target)

    def save_weights(self, target):
        np.savez(target, self.weights)

    def lookup(self, word, nullifabsent=True):
        if word in self.vocab:
            return self.weights[self.vocab[word]]
        if nullifabsent:
            return None
        return self.nullv

    def __getitem__(self, word):
        return self.lookup(word, nullifabsent=False)


@export
class PretrainedEmbeddingsModel(WordEmbeddingsModel):
    def __init__(self, filename, known_vocab=None, unif_weight=None, keep_unused=False, normalize=False, **kwargs):
        super(PretrainedEmbeddingsModel, self).__init__()

        if (known_vocab is None or not known_vocab) and keep_unused is False:
            logger.warning(
                "Warning: known_vocab=None or is Empty, keep_unused=False. Setting keep_unused=True, all vocab will be preserved"
            )
            keep_unused = True
        uw = 0.0 if unif_weight is None else unif_weight
        self.vocab = {}
        # Set the start offset to one past the last special token
        idx = Offsets.OFFSET

        word_vectors, self.dsz, known_vocab, idx = self._read_vectors(filename, idx, known_vocab, keep_unused, **kwargs)
        self.nullv = np.zeros(self.dsz, dtype=np.float32)
        special_tokens = [self.nullv]
        for i in range(1, len(Offsets.VALUES)):
            special_tokens.append(np.random.uniform(-uw, uw, self.dsz).astype(np.float32))
        word_vectors = special_tokens + word_vectors
        # Add "well-known" values to the vocab
        for i, name in enumerate(Offsets.VALUES):
            self.vocab[name] = i

        if known_vocab is not None:
            # Remove "well-known" values
            for name in Offsets.VALUES:
                known_vocab.pop(name, 0)
            unknown = {v: cnt for v, cnt in known_vocab.items() if cnt > 0}
            for v in unknown:
                word_vectors.append(np.random.uniform(-uw, uw, self.dsz).astype(np.float32))
                self.vocab[v] = idx
                idx += 1

        self.weights = np.array(word_vectors)
        if normalize is True:
            self.weights = norm_weights(self.weights)

        self.vsz = self.weights.shape[0]
        assert self.weights.dtype == np.float32

    def _read_vectors(self, filename, idx, known_vocab, keep_unused, **kwargs):
        use_mmap = bool(kwargs.get("use_mmap", False))
        read_fn = self._read_word2vec_file
        is_glove_file = mime_type(filename) == "text/plain"
        if use_mmap:
            if is_glove_file:
                read_fn = self._read_text_mmap
            else:
                read_fn = self._read_word2vec_mmap
        elif is_glove_file:
            read_fn = self._read_text_file

        return read_fn(filename, idx, known_vocab, keep_unused)

    def _read_word2vec_file(self, filename, idx, known_vocab, keep_unused):
        word_vectors = []
        with io.open(filename, "rb") as f:
            header = f.readline()
            vsz, dsz = map(int, header.split())
            width = 4 * dsz
            for i in range(vsz):
                word = self._readtospc(f)
                raw = f.read(width)
                if word in self.vocab:
                    continue
                if keep_unused is False and word not in known_vocab:
                    continue
                if known_vocab and word in known_vocab:
                    known_vocab[word] = 0
                vec = np.fromstring(raw, dtype=np.float32)
                word_vectors.append(vec)
                self.vocab[word] = idx
                idx += 1
        return word_vectors, dsz, known_vocab, idx

    @staticmethod
    def _read_word2vec_line_mmap(m, width, start):
        current = start + 1
        while m[current : current + 1] != b" ":
            current += 1
        vocab = m[start:current].decode("utf-8").strip(" \n")
        raw = m[current + 1 : current + width + 1]
        value = np.fromstring(raw, dtype=np.float32)
        return vocab, value, current + width + 1

    def _read_word2vec_mmap(self, filename, idx, known_vocab, keep_unused):
        import mmap

        word_vectors = []
        with io.open(filename, "rb") as f:
            with contextlib.closing(mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)) as m:
                header_end = m[:50].find(b"\n")
                vsz, dsz = map(int, (m[:header_end]).split(b" "))
                width = 4 * dsz
                current = header_end + 1
                for i in range(vsz):
                    word, vec, current = self._read_word2vec_line_mmap(m, width, current)
                    if word in self.vocab:
                        continue
                    if keep_unused is False and word not in known_vocab:
                        continue
                    if known_vocab and word in known_vocab:
                        known_vocab[word] = 0

                    word_vectors.append(vec)
                    self.vocab[word] = idx
                    idx += 1
                return word_vectors, dsz, known_vocab, idx

    @staticmethod
    def _readtospc(f):

        s = bytearray()
        ch = f.read(1)

        while ch != b" ":
            s.extend(ch)
            ch = f.read(1)
        s = s.decode("utf-8")
        # Only strip out normal space and \n not other spaces which are words.
        return s.strip(" \n")

    def _read_text_file(self, filename, idx, known_vocab, keep_unused):
        word_vectors = []

        with io.open(filename, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.rstrip("\n ")
                values = line.split(" ")
                word = values[0]
                if i == 0 and len(values) == 2:
                    print("VSZ: {}, DSZ: {}".format(word, values[1]))
                    continue
                if word in self.vocab:
                    continue
                if keep_unused is False and word not in known_vocab or word in self.vocab:
                    continue
                if known_vocab and word in known_vocab:
                    known_vocab[word] = 0
                vec = np.asarray(values[1:], dtype=np.float32)
                word_vectors.append(vec)
                self.vocab[word] = idx
                idx += 1
        dsz = vec.shape[0]
        return word_vectors, dsz, known_vocab, idx

    def _read_text_mmap(self, filename, idx, known_vocab, keep_unused):
        import mmap

        word_vectors = []
        with io.open(filename, "r", encoding="utf-8") as f:
            with contextlib.closing(mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)) as m:
                for i, line in enumerate(iter(m.readline, b"")):
                    line = line.rstrip(b"\n")
                    values = line.split(b" ")
                    if i == 0 and len(values) == 2:
                        print("VSZ: {}, DSZ: {}".format(values[0], values[1]))
                        continue
                    if len(values) == 0:
                        break
                    word = values[0].decode("utf-8").strip(" \n")
                    if word in self.vocab:
                        continue
                    if keep_unused is False and word not in known_vocab:
                        continue
                    if known_vocab and word in known_vocab:
                        known_vocab[word] = 0
                    vec = np.asarray(values[1:], dtype=np.float32)
                    word_vectors.append(vec)
                    self.vocab[word] = idx
                    idx += 1
                dsz = vec.shape[0]
                return word_vectors, dsz, known_vocab, idx


@export
class RandomInitVecModel(EmbeddingsModel):
    def __init__(self, dsz, known_vocab, counts=True, unif_weight=None):
        super(RandomInitVecModel, self).__init__()
        uw = 0.0 if unif_weight is None else unif_weight
        self.vocab = dict()
        for i, name in enumerate(Offsets.VALUES):
            self.vocab[name] = i
        self.dsz = dsz
        self.vsz = Offsets.OFFSET

        if counts is True:
            for name in Offsets.VALUES:
                known_vocab.pop(name, 0)
            attested = [v for v, cnt in known_vocab.items() if cnt > 0]
            for k, v in enumerate(attested):
                self.vocab[v] = k + Offsets.OFFSET
                self.vsz += 1
        else:
            self.vocab = known_vocab
            self.vsz = len(self.vocab)

        self.weights = np.random.uniform(-uw, uw, (self.vsz, self.dsz)).astype(np.float32)

        self.nullv = np.zeros(self.dsz, dtype=np.float32)

        self.weights[0] = self.nullv
        for i in range(1, len(Offsets.VALUES)):
            self.weights[i] = np.random.uniform(-uw, uw, self.dsz).astype(np.float32)

    def __getitem__(self, word):
        return self.lookup(word, nullifabsent=False)

    def lookup(self, word, nullifabsent=True):
        if word in self.vocab:
            return self.weights[self.vocab[word]]
        if nullifabsent:
            return None
        return self.nullv

    def get_vocab(self):
        return self.vocab

    def get_dsz(self):
        return self.dsz

    def get_vsz(self):
        return self.vsz

    def save_md(self, target):
        write_json({"vsz": self.get_vsz(), "dsz": self.get_dsz(), "vocab": self.get_vocab()}, target)


MEAD_LAYERS_EMBEDDINGS = {}
MEAD_LAYERS_EMBEDDINGS_LOADERS = {}


@export
@optional_params
def register_embeddings(cls, name=None):
    """Register a function as a plug-in"""
    if name is None:
        name = cls.__name__

    if name in MEAD_LAYERS_EMBEDDINGS:
        raise Exception(
            "Error: attempt to re-define previously registered handler {} (old: {}, new: {}) in registry".format(
                name, MEAD_LAYERS_EMBEDDINGS[name], cls
            )
        )

    MEAD_LAYERS_EMBEDDINGS[name] = cls

    if hasattr(cls, "load"):
        MEAD_LAYERS_EMBEDDINGS_LOADERS[name] = cls.load
    return cls


@export
def create_embeddings(**kwargs):
    embed_type = kwargs.get("embed_type", "default")
    Constructor = MEAD_LAYERS_EMBEDDINGS.get(embed_type)
    return Constructor(**kwargs)


@export
def load_embeddings(name, **kwargs):
    """This method negotiates loading an embeddings sub-graph AND a corresponding vocabulary (lookup from word to int)

    This function behaves differently depending on its keyword arguments and the `embed_type`.
    If the registered embeddings class contains a load method on it and we are given an `embed_file`,
    we will assume that we need to load that file, and that the embeddings object wants its own load function used
    for that.  This would be typical, e.g, for a user-defined sub-graph LM.

    For cases where no `embed_file` is provided and there is a `create` method on this class, we  assume that the user
    wants us to build a VSM (`baseline.w2v`) ourselves, and call their create function, which will take in this VSM.
    The VSM is then used to provide the vocabulary back, and the `create` function invokes the class constructor
    with the sub-parts of VSM required to build the graph.

    If there is no create method provided, and there is no load function provided, we simply invoke the regsitered embeddings'
    constructor with the args, and assume there is a `get_vocab()` method on the provided implementation

    :param name: (``str``) A unique string name for these embeddings
    :param kwargs:
    :return:
    """
    embed_type = kwargs.pop("embed_type", "default")
    embeddings_cls = MEAD_LAYERS_EMBEDDINGS[embed_type]

    filename = kwargs.get("embed_file")

    # If the class has a load function, we are going to use that
    if hasattr(embeddings_cls, "load") and filename is not None:
        model = embeddings_cls.load(filename, **kwargs)
        return {"embeddings": model, "vocab": model.get_vocab()}

    elif hasattr(embeddings_cls, "create"):
        unif = kwargs.pop("unif", 0.1)
        known_vocab = kwargs.pop("known_vocab", None)
        keep_unused = kwargs.pop("keep_unused", False)
        normalize = kwargs.pop("normalized", False)
        # if there is no filename, use random-init model
        if filename is None:
            dsz = kwargs.pop("dsz")
            model = RandomInitVecModel(dsz, known_vocab=known_vocab, unif_weight=unif)
        # If there, is use hte pretrain loader
        else:
            model = PretrainedEmbeddingsModel(
                filename,
                known_vocab=known_vocab,
                unif_weight=unif,
                keep_unused=keep_unused,
                normalize=normalize,
                **kwargs,
            )

        # Then call create(model, name, **kwargs)
        return {"embeddings": embeddings_cls.create(model, name, **kwargs), "vocab": model.get_vocab()}
    # If we dont have a load function, but filename is none, we should just instantiate the class
    model = embeddings_cls(name, **kwargs)
    return {"embeddings": model, "vocab": model.get_vocab()}
