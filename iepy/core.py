# -*- coding: utf-8 -*-
from collections import defaultdict, namedtuple
import itertools

from colorama import Fore, Style

from iepy import db
from iepy.fact_extractor import FactExtractorFactory

from iepy.fact_extractor import (
    bag_of_words,
    bag_of_pos,
    bag_of_word_bigrams,
    bag_of_words_in_between,
    bag_of_pos_in_between,
    entity_order,
    entity_distance,
    other_entities_in_between,
    in_same_sentence,
    verbs_count_in_between,
    verbs_count,
    total_number_of_entities,
    symbols_in_between,
    number_of_tokens,
    BagOfVerbStems,
    BagOfVerbLemmas,
)

# A fact is a triple with two Entity() instances and a relation label
Fact = namedtuple("Fact", "e1 relation e2")

# An Evidence is a pair of a Fact and a TextSegment and occurrence indices
# The segment+indices can be left out (as None)
# The following invariants apply
#   - e.segment == None iff e.o1 == None
#   - e.segment == None iff e.o2 == None
#   - e.o1 != None implies e.fact.e1.kind == e.segment.entities[e.o1].kind and e.fact.e1.key == e.segment.entities[e.o1].key
#   - e.o2 != None implies e.fact.e2.kind == e.segment.entities[e.o2].kind and e.fact.e2.key == e.segment.entities[e.o2].key
BaseEvidence = namedtuple("Evidence", "fact segment o1 o2")


class Evidence(BaseEvidence):
    __slots__ = []

    def colored_text(self, color_1, color_2):
        """Will return a naive formated text with entities remarked.
        Assumes that occurrences does not overlap.
        """
        occurr1 = self.segment.entities[self.o1]
        occurr2 = self.segment.entities[self.o2]
        tkns = self.segment.tokens[:]
        if self.o1 < self.o2:
            tkns.insert(occurr2.offset_end, Style.RESET_ALL)
            tkns.insert(occurr2.offset, color_2)
            tkns.insert(occurr1.offset_end, Style.RESET_ALL)
            tkns.insert(occurr1.offset, color_1)
        else:  # must be solved in the reverse order
            tkns.insert(occurr1.offset_end, Style.RESET_ALL)
            tkns.insert(occurr1.offset, color_1)
            tkns.insert(occurr2.offset_end, Style.RESET_ALL)
            tkns.insert(occurr2.offset, color_2)
        return u' '.join(tkns)

    def colored_fact(self, color_1, color_2):
        return u'(%s, %s, %s)' % (
            color_1 + self.fact.e1.key + Style.RESET_ALL,
            self.fact.relation,
            color_2 + self.fact.e2.key + Style.RESET_ALL,
        )

    def colored_fact_and_text(self):
        color_1 = Fore.RED
        color_2 = Fore.GREEN
        return (
            self.colored_fact(color_1, color_2),
            self.colored_text(color_1, color_2)
        )


def certainty(p):
    return 0.5 + abs(p - 0.5) if p is not None else 0.5


class Knowledge(dict):
    """Maps evidence to a score in [0...1]

    None is also a valid score for cases when no score information is available
    """
    __slots__ = ()

    def by_certainty(self):
        """
        Returns an iterable over the evidence, with the most certain evidence
        at the front and the least certain evidence at the back. "Certain"
        means a score close to 0 or 1, and "uncertain" a score closer to 0.5.
        Note that a score of 'None' is considered as 0.5 here
        """
        def key_funct(e_s):
            e = e_s[0]
            return (certainty(self[e]) if self[e] is not None else 0, e)
        return sorted(self.items(), key=key_funct, reverse=True)

    def per_relation(self):
        """
        Returns a dictionary: relation -> Knowledge, where each value is only
        the knowledge for that specific relation
        """
        result = defaultdict(Knowledge)
        for e, s in self.items():
            result[e.fact.relation][e] = s
        return result


class BootstrappedIEPipeline(object):
    """
    From the user's point of view this class is meant to be used like this:
        p = BoostrappedIEPipeline(db_connector, seed_facts)
        p.start()  # blocking
        while UserIsNotTired:
            for question in p.questions_available():
                # Ask user
                # ...
                p.add_answer(question, answer)
            p.force_process()
        facts = p.get_facts()  # profit
    """

    def __init__(self, db_connector, seed_facts):
        """
        Not blocking.
        """
        self.db_con = db_connector
        self.knowledge = Knowledge({Evidence(f, None, None, None): 1 for f in seed_facts})
        self.evidence_threshold = 0.99
        self.fact_threshold = 0.99
        self.questions = Knowledge()
        self.answers = {}

        self.steps = [
                self.generalize_knowledge,    # Step 1
                self.generate_questions,     # Step 2, first half
                None,                        # Pause to wait question answers
                self.filter_evidence,        # Step 2, second half
                self.learn_fact_extractors,  # Step 3
                self.extract_facts,          # Step 5
                self.filter_facts            # Step 6
            ]
        self.step_iterator = itertools.cycle(self.steps)

        # Build relation description: a map from relation labels to pairs of entity kinds
        self.relations = {}
        for e in self.knowledge:
            t1 = e.fact.e1.kind
            t2 = e.fact.e1.kind
            if e.fact.relation in self.relations and (t1, t2) != self.relations[e.fact.relation]:
                raise ValueError("Ambiguous kinds for relation %r" % e.fact.relation)
            self.relations[e.fact.relation] = (t1, t2)
        # Classifier configuration
        self.extractor_config = {
            "classifier": "dtree",
            "classifier_args": dict(),
            "dimensionality_reduction": None,
            "scaler": False,
            "column_filter": False,
            "features": [
                bag_of_words,
                bag_of_pos,
                bag_of_word_bigrams,
                bag_of_words_in_between,
                bag_of_pos_in_between,
                entity_order,
                entity_distance,
                other_entities_in_between,
                in_same_sentence,
                verbs_count_in_between,
                verbs_count,
                total_number_of_entities,
                symbols_in_between,
                number_of_tokens,
                BagOfVerbStems(in_between=True),
                BagOfVerbStems(in_between=False),
                BagOfVerbLemmas(in_between=True),
                BagOfVerbLemmas(in_between=False)
            ]
        }


    def do_iteration(self, data):
        for step in self.step_iterator:
            if step is None:
                return
            data = step(data)

    ###
    ### IEPY User API
    ###

    def start(self):
        """
        Blocking.
        """
        self.do_iteration(self.knowledge)

    def questions_available(self):
        """
        Not blocking.
        Returned value won't change until a call to `add_answer` or
        `force_process`.
        If `id` of the returned value hasn't changed the returned value is the
        same.
        The questions avaiable are a list of evidence.
        """
        return self.questions.by_certainty()

    def add_answer(self, evidence, answer):
        """
        Blocking (potentially).
        After calling this method the values returned by `questions_available`
        and `known_facts` might change.
        """
        self.answers[evidence] = int(answer)

    def force_process(self):
        """
        Blocking.
        After calling this method the values returned by `questions_available`
        and `known_facts` might change.
        """
        self.do_iteration(None)

    def known_facts(self):
        """
        Not blocking.
        Returned value won't change until a call to `add_answer` or
        `force_process`.
        If `len` of the returned value hasn't changed the returned value is the
        same.
        """
        return self.knowledge

    ###
    ### Pipeline steps
    ###

    def generalize_knowledge(self, evidence):
        """
        Stage 1 of pipeline.
        """
        return Knowledge(
            (Evidence(fact, segment, o1, o2), evidence.get(Evidence(fact, segment, o1, o2)))
            for fact, _s, _o1, _o2 in self.knowledge
            for segment in self.db_con.segments.segments_with_both_entities(fact.e1, fact.e2)
            for o1, o2 in segment.entity_occurrence_pairs(fact.e1, fact.e2)
        )

    def generate_questions(self, evidence):
        """
        Pseudocode. Stage 2.1 of pipeline.
        confidence can implemented using the output from step 5 or accessing
        the classifier in step 3.

        Stores questions in self.questions and stops
        """
        self.questions = Knowledge((e, s) for e, s in evidence.items() if e not in self.answers)

    def filter_evidence(self, _):
        """
        Pseudocode. Stage 2.2 of pipeline.
        sorted_evidence is [(score, segment, (a, b, relation)), ...]
        answers is {(segment, (a, b, relation)): is_evidence, ...}
        """
        evidence = Knowledge(self.answers)
        evidence.update(
            (e, score > 0.5)
            for e, score in self.questions.items()
            if certainty(score) > self.evidence_threshold and e not in self.answers
        )
        # Answers + questions with a strong prediction
        return evidence

    def learn_fact_extractors(self, evidence):
        """
        Stage 3 of pipeline.
        evidence is a Knowledge instance of {evidence: is_good_evidence}
        """
        classifiers = {}
        for rel, k in evidence.per_relation().items():
            classifiers[rel] = FactExtractorFactory(self.extractor_config, k)
        return classifiers

    def extract_facts(self, extractors):
        """
        Pseudocode. Stage 5 of pipeline.
        extractors is a dict {relation: classifier, ...}
        """
        # TODO: this probably is smarter as an outer iteration through segments
        # and then an inner iteration over relations

        result = Knowledge()

        for r in extractors:
            lkind, rkind = self.relations[r]
            evidence = []
            for segment in self.db_con.segments.segments_with_both_kinds(lkind, rkind):
                for o1, o2 in segment.kind_occurrence_pairs(lkind, rkind):
                    e1 = db.get_entity(segment.entities[o1].kind, segment.entities[o1].key)
                    e2 = db.get_entity(segment.entities[o2].kind, segment.entities[o2].key)
                    f = Fact(e1, r, e2)
                    e = Evidence(f, segment, o1, o2)
                    evidence.append(e)
            result.update(zip(evidence, extractors[r].predict(evidence)))
        return result

    def filter_facts(self, facts):
        """
        Pseudocode. Stage 6 of pipeline.
        facts is [((a, b, relation), confidence), ...]
        """
        self.knowledge.update((e, s) for e, s in facts.items() if certainty(s) > self.fact_threshold)
        return facts

    ###
    ### Aux methods
    ###
    def _confidence(self, evidence):
        """
        Returns a probability estimation of segment being an manifestation of
        fact.
        fact is (a, b, relation).
        """
        if evidence in self.knowledge:
            return self.knowledge[evidence]

        # FIXME: to be implemented on ticket IEPY-47
        return 0.5


def _all_entity_pairs(segment):
    """
    Aux method, returns all entity pairs in a segment.
    Order is important, so expect (a, b) and (b, a) in the answer.
    """
    raise NotImplementedError


def _relation_is_compatible(a, b, relation):
    """
    Aux method, returns True if a and b have types compatible with
    relation.
    """
    raise NotImplementedError