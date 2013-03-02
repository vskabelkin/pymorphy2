# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals, division
import operator
import logging

from .utils import word_splits
from .shapes import is_latin, is_punctuation

logger = logging.getLogger(__name__)

__all__ = [
    "KnownPrefixPredictor",
    "UnknownPrefixPredictor",
    "KnownSuffixPredictor",
    "HyphenSeparatedParticlePredictor",
    "HyphenatedWordsPredictor",
    "PunctuationPredictor",
    "LatinPredictor",
]

class BasePredictor(object):

    terminal = False

    def __init__(self, morph):
        """
        @type morph: pymorphy2.analyzer.MorphAnalyzer
        @type self.dict: pymorphy2.analyzer.Dictionary
        """
        self.morph = morph
        self.dict = morph.dictionary

    def parse(self, word, seen_parses):
        raise NotImplementedError()

    def tag(self, word, seen_tags):
        raise NotImplementedError()

    def get_lexeme(self, form, methods):
        # be default, predictor gets a lexeme from a previous predictor:
        assert methods[-1][0] is self
        if len(methods) == 1:
            return self.dict.get_lexeme(form, [])

        assert len(methods) > 1, len(methods)
        previous_predictor = methods[-2][0]
        return previous_predictor.get_lexeme(form, methods[:-1])

    def normalized(self, form):
        return self.dict.normalized(form)

    def __repr__(self):
        return str("<%s>") % self.__class__.__name__


class KnownPrefixPredictor(BasePredictor):
    """
    Parse the word by checking if it starts with a known prefix
    and parsing the reminder.
    """

    terminal = True
    ESTIMATE_DECAY = 0.75
    MIN_REMINDER_LENGTH = 3

    def _word_prefixes(self, word):
        return sorted(
            self.dict.prediction_prefixes.prefixes(word),
            key=len,
            reverse=True,
        )

    def parse(self, word, seen_parses):
        result = []
        for prefix in self._word_prefixes(word):
            unprefixed_word = word[len(prefix):]

            if len(unprefixed_word) < self.MIN_REMINDER_LENGTH:
                continue

            method = (self, prefix)

            for fixed_word, tag, normal_form, para_id, idx, estimate, methods in self.morph.parse(unprefixed_word):

                if not tag.is_productive():
                    continue

                parse = (
                    prefix+fixed_word, tag, prefix+normal_form,
                    para_id, idx, estimate*self.ESTIMATE_DECAY,
                    methods+(method,)
                )

                _add_parse_if_not_seen(parse, result, seen_parses)

        return result

    def tag(self, word, seen_tags):
        result = []
        for prefix in self._word_prefixes(word):
            unprefixed_word = word[len(prefix):]

            if len(unprefixed_word) < self.MIN_REMINDER_LENGTH:
                continue

            for tag in self.morph.tag(unprefixed_word):
                if not tag.is_productive():
                    continue
                _add_tag_if_not_seen(tag, result, seen_tags)

        return result


class UnknownPrefixPredictor(BasePredictor):
    """
    Parse the word by parsing only the word suffix
    (with restrictions on prefix & suffix lengths).
    """
    terminal = False
    ESTIMATE_DECAY = 0.5

    def parse(self, word, seen_parses):
        result = []
        for prefix, unprefixed_word in word_splits(word):

            method = (self, prefix)

            for fixed_word, tag, normal_form, para_id, idx, estimate, methods in self.dict.parse(unprefixed_word):

                if not tag.is_productive():
                    continue

                parse = (prefix+fixed_word, tag, prefix+normal_form,
                         para_id, idx, estimate*self.ESTIMATE_DECAY,
                         methods+(method,))
                _add_parse_if_not_seen(parse, result, seen_parses)

        return result

    def tag(self, word, seen_tags):
        result = []
        for _, unprefixed_word in word_splits(word):
            for tag in self.dict.tag(unprefixed_word):

                if not tag.is_productive():
                    continue

                _add_tag_if_not_seen(tag, result, seen_tags)

        return result


class KnownSuffixPredictor(BasePredictor):
    """
    Parse the word by checking how the words with similar suffixes
    are parsed.
    """
    terminal = False
    ESTIMATE_DECAY = 0.5

    def __init__(self, morph):
        super(KnownSuffixPredictor, self).__init__(morph)

        self._paradigm_prefixes = list(reversed(list(enumerate(self.dict.paradigm_prefixes))))
        max_suffix_length = self.dict.meta['prediction_options']['max_suffix_length']
        self._prediction_splits = list(reversed(range(1, max_suffix_length+1)))


    def parse(self, word, seen_parses):
        result = []

        # smoothing; XXX: isn't max_cnt better?
        total_counts = [1] * len(self._paradigm_prefixes)

        for prefix_id, prefix in self._paradigm_prefixes:

            if not word.startswith(prefix):
                continue

            suffixes_dawg = self.dict.prediction_suffixes_dawgs[prefix_id]

            for i in self._prediction_splits:
                end = word[-i:]  # XXX: this should be counted once, not for each prefix
                para_data = suffixes_dawg.similar_items(end, self.dict.ee)

                for fixed_suffix, parses in para_data:
                    method = (self, fixed_suffix)

                    for cnt, para_id, idx in parses:
                        tag = self.dict.build_tag_info(para_id, idx)

                        if not tag.is_productive():
                            continue
                        total_counts[prefix_id] += cnt

                        fixed_word = word[:-i] + fixed_suffix
                        normal_form = self.dict.build_normal_form(para_id, idx, fixed_word)

                        parse = (cnt, fixed_word, tag, normal_form,
                                 para_id, idx, prefix_id, (method,))
                        reduced_parse = parse[1:4]
                        if reduced_parse in seen_parses:
                            continue

                        result.append(parse)

                if total_counts[prefix_id] > 1:
                    break

        result = [
            (fixed_word, tag, normal_form, para_id, idx, cnt/total_counts[prefix_id] * self.ESTIMATE_DECAY, methods)
            for (cnt, fixed_word, tag, normal_form, para_id, idx, prefix_id, methods) in result
        ]
        result.sort(key=operator.itemgetter(5), reverse=True)
        return result


    def tag(self, word, seen_tags):
        # XXX: the result order may be different from
        # ``self.parse(...)``.

        result = []

        for prefix_id, prefix in self._paradigm_prefixes:

            if not word.startswith(prefix):
                continue

            suffixes_dawg = self.dict.prediction_suffixes_dawgs[prefix_id]

            for i in self._prediction_splits:
                end = word[-i:]  # XXX: this should be counted once, not for each prefix
                para_data = suffixes_dawg.similar_items(end, self.dict.ee)
                found = False

                for fixed_suffix, parses in para_data:
                    for cnt, para_id, idx in parses:

                        tag = self.dict.build_tag_info(para_id, idx)

                        if not tag.is_productive():
                            continue

                        found = True
                        if tag in seen_tags:
                            continue
                        seen_tags.add(tag)
                        result.append((cnt, tag))

                if found:
                    break

        result.sort(reverse=True)
        return [tag for cnt, tag in result]


class HyphenSeparatedParticlePredictor(BasePredictor):
    """
    Parse the word by analyzing it without
    a particle after a hyphen (tokens like "смотри-ка").

    .. note::

        This predictor doesn't remove particles from the result
        so for normalization you may need to handle
        particles at tokenization level.

    """
    terminal = True
    ESTIMATE_DECAY = 0.9

    # XXX: maybe the code can be made faster by compiling this list to a DAWG?
    PARTICLES_AFTER_HYPHEN = [
        "-то", "-ка", "-таки", "-де", "-тко", "-тка", "-с", "-ста"
    ]

    def get_lexeme(self, form, methods):
        particle = methods[-1][1]

        return list(
            self._suffixed_lexeme(
                super(HyphenSeparatedParticlePredictor, self).get_lexeme(
                    self._unsuffixed_form(form, particle),
                    methods
                ),
                particle
            )
        )

    def _suffixed_lexeme(self, lexeme, suffix):
        for p in lexeme:
            word, tag, normal_form, para_id, idx, estimate, methods = p
            yield (word+suffix, tag, normal_form+suffix,
                   para_id, idx, estimate, methods)

    def _unsuffixed_form(self, form, suffix):
        word, tag, normal_form, para_id, idx, estimate, methods = form
        return (word[:-len(suffix)], tag, normal_form[:-len(suffix)],
                para_id, idx, estimate, methods)


    def parse(self, word, seen_parses):

        result = []
        for particle in self.PARTICLES_AFTER_HYPHEN:
            if not word.endswith(particle):
                continue

            unsuffixed_word = word[:-len(particle)]
            if not unsuffixed_word:
                continue

            method = (self, particle)

            for fixed_word, tag, normal_form, para_id, idx, estimate, methods in self.morph.parse(unsuffixed_word):
                parse = (
                    fixed_word+particle, tag, normal_form+particle,
                    para_id, idx, estimate*self.ESTIMATE_DECAY,
                    methods+(method,)
                )
                _add_parse_if_not_seen(parse, result, seen_parses)

            # If a word ends with with one of the particles,
            # it can't ends with an another.
            break

        return result


    def tag(self, word, seen_tags):
        result = []
        for particle in self.PARTICLES_AFTER_HYPHEN:
            if not word.endswith(particle):
                continue

            unsuffixed_word = word[:-len(particle)]
            if not unsuffixed_word:
                continue

            result.extend(self.morph.tag(unsuffixed_word))

            # If a word ends with with one of the particles,
            # it can't ends with an another.
            break

        return result


class HyphenatedWordsPredictor(BasePredictor):
    """
    Parse the word by parsing its hyphen-separated parts.
    """

    terminal = True
    ESTIMATE_DECAY = 0.75

    def _similarity_features(self, tag):
        """
        @type tag: pymorphy2.tagset.OpencorporaTag
        """
        return (tag.POS, tag.number, tag.case, tag.person, tag.tense)

    def parse(self, word, seen_parses):
        if '-' not in word:
            return []

        result = []

        # If there are more than 2 parts, the rest would be parsed
        # by recursion.
        left, right = word.split('-', 1)

        left_parses = self.morph.parse(left)
        right_parses = self.morph.parse(right)

        # Step 1: Assume that the left part is an uninflected prefix.
        # Examples: интернет-магазин, воздушно-капельный
        method1 = (self, right)
        right_features = []

        for fixed_word, tag, normal_form, para_id, idx, estimate, methods in right_parses:
            parse = (
                '-'.join([left, fixed_word]), tag, '-'.join([left, normal_form]),
                para_id, idx, estimate*self.ESTIMATE_DECAY,
                methods+(method1,)
            )
            _add_parse_if_not_seen(parse, result, seen_parses)
            right_features.append(self._similarity_features(tag))

        # Step 2: if left and right can be parsed the same way,
        # then it may be the case that both parts should be inflected.
        # Examples: человек-гора, команд-участниц, компания-производитель

        method2 = (self, word)

        # FIXME: quadratic algorithm
        for left_parse in left_parses:

            left_feat = self._similarity_features(left_parse[1])

            for parse_index, right_parse in enumerate(right_parses):
                right_feat = right_features[parse_index]

                if left_feat != right_feat:
                    continue

                # tag
                parse = (
                    '-'.join([left_parse[0], right_parse[0]]), # word
                    left_parse[1], # tag is from the left part
                    '-'.join([left_parse[2], right_parse[2]]),  # normal form
                    left_parse[3], left_parse[4], # para_id, idx?
                    left_parse[5]*self.ESTIMATE_DECAY,
                    left_parse[6]+(method2,)
                )
                _add_parse_if_not_seen(parse, result, seen_parses)

        return result

    def tag(self, word, seen_tags):
        result = []
        for p in self.parse(word, set()):
            _add_tag_if_not_seen(p[1], result, seen_tags)
        return result




class _ShapeAnalyzer(BasePredictor):
    ESTIMATE = 0.5
    EXTRA_GRAMMEMES = []

    def __init__(self, morph):
        super(_ShapeAnalyzer, self).__init__(morph)
        self.morph.TagClass.KNOWN_GRAMMEMES.update(self.EXTRA_GRAMMEMES)

    def _check_shape(self, word):
        raise NotImplementedError()

    def _get_tag(self, word, shape):
        raise NotImplementedError()

    def parse(self, word, seen_parses):
        shape = self._check_shape(word)
        if not shape:
            return []

        return [(
            word, self._get_tag(word, shape), word,
            None, None, self.ESTIMATE,
            [(self, )],
        )]

    def tag(self, word, seen_tags):
        shape = self._check_shape(word)
        if not shape:
            return []
        return [self._get_tag(word, shape)]

    def get_lexeme(self, form, methods):
        return [form]

    def normalized(self, form):
        return form


class PunctuationPredictor(_ShapeAnalyzer):
    """
    This predictor tags punctuation marks as "PNCT".
    """
    terminal = True
    ESTIMATE = 0.5
    EXTRA_GRAMMEMES = ['PNCT']

    def __init__(self, morph):
        super(PunctuationPredictor, self).__init__(morph)
        self._tag = self.morph.TagClass('PNCT')

    def _get_tag(self, word, shape):
        return self._tag

    def _check_shape(self, word):
        return is_punctuation(word)


class LatinPredictor(_ShapeAnalyzer):
    """
    This predictor marks latin words with "LATN" tag.
    """
    terminal = True
    ESTIMATE = 0.5
    EXTRA_GRAMMEMES = ['LATN']

    def __init__(self, morph):
        super(LatinPredictor, self).__init__(morph)
        self._tag = self.morph.TagClass('LATN')

    def _get_tag(self, word, shape):
        return self._tag

    def _check_shape(self, word):
        return is_latin(word)


def _add_parse_if_not_seen(parse, result_list, seen_parses):
    reduced_parse = parse[:3]
    if reduced_parse in seen_parses:
        return
    seen_parses.add(reduced_parse)
    result_list.append(parse)

def _add_tag_if_not_seen(tag, result_list, seen_tags):
    if tag in seen_tags:
        return
    seen_tags.add(tag)
    result_list.append(tag)
