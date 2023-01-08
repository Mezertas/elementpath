#
# Copyright (c), 2018-2021, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
# type: ignore
"""
XPath 3.1 implementation - part 2 (operators and constructors)
"""
from ..helpers import iter_sequence
from ..sequence_types import match_sequence_type
from ..xpath_tokens import XPathToken, ProxyToken, XPathFunction, XPathMap, XPathArray
from .xpath31_parser import XPath31Parser

register = XPath31Parser.register
method = XPath31Parser.method
function = XPath31Parser.function

# register('map', bp=90, label='map', bases=(XPathMap,))
# register('array', bp=90, label='array', bases=(XPathArray,))

register('map', bp=90, label=('kind test', 'map'), bases=(XPathFunction,),
         pattern=r'(?<!\$)\bmap(?=\s*(?:\(\:.*\:\))?\s*(?=\(|\{)(?!\:))')


@method('map')
def nud_map_sequence_type_or_constructor(self):
    if self.parser.next_token.symbol == '{':
        self.parser.token = XPathMap(self.parser).nud()
        return self.parser.token
    elif self.parser.next_token.symbol != '(':
        return self.as_name()

    self.label = 'kind test'

    self.parser.advance('(')
    if self.parser.next_token.label not in ('kind test', 'sequence type', 'function test'):
        self.parser.expected_next('(name)', ':', '*', message='a QName or a wildcard expected')
    self[:] = self.parser.expression(45),
    self[0].parse_occurrence()

    if self[0].symbol != '*':
        self.parser.advance(',')
        if self.parser.next_token.label not in ('kind test', 'sequence type', 'function test'):
            self.parser.expected_next('(name)', ':', '*', message='a QName or a wildcard expected')
        self.append(self.parser.expression(45))
        self[-1].parse_occurrence()

    self.parser.advance(')')
    self.value = None
    return self


register('array', bp=90, label=('kind test', 'array'), bases=(XPathFunction,),
         pattern=r'(?<!\$)\barray(?=\s*(?:\(\:.*\:\))?\s*(?=\(|\{)(?!\:))')


@method('array')
def nud_sequence_type_or_curly_array_constructor(self):
    if self.parser.next_token.symbol == '{':
        self.parser.token = XPathArray(self.parser).nud()
        return self.parser.token
    elif self.parser.next_token.symbol != '(':
        return self.as_name()

    self.label = 'kind test'
    self.parser.advance('(')
    if self.parser.next_token.label != 'kind test':
        self.parser.expected_next('(name)', ':', '*', message='a QName or a wildcard expected')
    self[:] = self.parser.expression(45),
    if self[0].symbol != '*':
        self[0].parse_occurrence()
    self.parser.advance(')')
    self.parse_occurrence()
    self.value = None
    return self


@method('map')
@method('array')
def select_map_or_array_kind_test(self, context=None):
    if context is None:
        raise self.missing_context()

    for item in context.iter_children_or_self():
        if match_sequence_type(item, self.source, self.parser):
            yield item


###
# Square array constructor (pushed lazy)
@method('[')
def nud_square_array_constructor(self):
    if self.parser.version < '3.1':
        raise self.wrong_syntax()

    # Constructs an XPathArray token and returns it instead of the predicate
    token = XPathArray(self.parser)
    token.symbol = '['
    if token.parser.next_token.symbol not in (']', '(end)'):
        while True:
            token.append(self.parser.expression(5))
            if token.parser.next_token.symbol != ',':
                break
            token.parser.advance()

    token.parser.advance(']')
    return token


class LookupOperatorToken(XPathToken):
    """
    Question mark symbol is used for XP31+ lookup operator and also for
    placeholder in XP30+ partial functions and for optional occurrences.
    """
    symbol = lookup_name = '?'
    lbp = 85
    rbp = 85

    def __init__(self, parser, value=None):
        super().__init__(parser, value)

        if self.parser.token.symbol == '(name)':
            pass  # self.lbp = 6
        elif self.parser.token.symbol in ('(', ','):
            self.lbp = self.rbp = 0  # It's a placeholder symbol

    def nud(self):
        try:
            self.parser.expected_next('(name)', '(integer)', '(', '*')
        except SyntaxError:
            return self  # a placeholder token
        else:
            self[:] = self.parser.expression(85),
            return self

    def led(self, left):
        self.parser.expected_next('(name)', '(integer)', '(', '*')
        self[:] = left, self.parser.expression(85)
        return self

    def evaluate(self, context=None):
        if not self:
            return self.value  # a placeholder token
        return [x for x in self.select(context)]

    def select(self, context=None):
        if not self:
            yield from iter_sequence(self.value)
            return

        if len(self) == 1:
            # unary lookup operator (used in predicates)
            if context is None:
                raise self.missing_context()
            items = (context.item,)
        else:
            items = self[0].select(context)

        for item in items:
            symbol = self[-1].symbol
            if isinstance(item, XPathMap):
                if symbol == '*':
                    for value in item.values(context):
                        yield from iter_sequence(value)
                elif symbol in ('(name)', '(integer)'):
                    yield from iter_sequence(item(context, self[-1].value))
                elif symbol == '(':
                    for value in self[-1].select(context):
                        yield from iter_sequence(item(context, self.data_value(value)))

            elif isinstance(item, XPathArray):
                if symbol == '*':
                    yield from item.items(context)
                elif symbol == '(name)':
                    raise self.error('XPTY0004')
                elif symbol == '(integer)':
                    yield item(context, self[-1].value)
                elif symbol == '(':
                    for value in self[-1].select(context):
                        yield item(context, self.data_value(value))

            else:
                raise self.error('XPTY0004')


XPath31Parser.symbol_table['?'] = LookupOperatorToken


@method('=>', bp=67)
def led_arrow_operator(self, left):
    next_token = self.parser.next_token
    if next_token.symbol == '$':
        self[:] = left, self.parser.expression(80)
    elif isinstance(next_token, ProxyToken):
        self.parser.parse_arguments = False
        self[:] = left, next_token.nud()
        self.parser.parse_arguments = True
        self.parser.advance()
    elif isinstance(next_token, XPathFunction):
        self[:] = left, next_token
        self.parser.advance()  # Skip static evaluation of function arguments
    else:
        next_token.expected('(name)', ':', 'Q{', '(')
        self.parser.parse_arguments = False
        self[:] = left, self.parser.expression(80)
        self.parser.parse_arguments = True

    right = self.parser.expression(67)
    right.expected('(')
    self.append(right)
    return self


@method('=>')
def evaluate_arrow_operator(self, context=None):
    arguments = [self[0].evaluate(context)]
    if self[2]:
        other_args = self[2].evaluate(context)
        if isinstance(other_args, list):
            arguments.extend(other_args)
        else:
            arguments.append(other_args)

    func = self[1].get_function(context, arity=len(arguments))
    return func(context, *arguments)
