#!/usr/bin/env python3
#
# Copyright (c), 2018-2020, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Jelte Jansen <github@tjeb.nl>
# @author Davide Brunato <brunato@sissa.it>
#
"""
Tests script for running W3C XPath tests. A reworking of
https://github.com/tjeb/elementpath_w3c_tests that uses
ElementTree and only essential parts.
"""
import argparse
import contextlib
import decimal
import re
import json
import math
import os
import sys
import traceback

from collections import OrderedDict
from xml.etree import ElementTree

from elementpath import ElementPathError, XPath2Parser, XPathContext
import elementpath


IGNORE_DEPENDENCIES = {'XQ10', 'XQ10+', 'XP30', 'XP30+', 'XQ30',
                       'XQ30+', 'XP31', 'XP31+', 'XQ31', 'XQ31+'}

SKIP_TESTS = [
    'fn-subsequence__cbcl-subsequence-010',
    'fn-subsequence__cbcl-subsequence-011',
    'fn-subsequence__cbcl-subsequence-012',
    'fn-subsequence__cbcl-subsequence-013',
    'fn-subsequence__cbcl-subsequence-014',
    'prod-NameTest__NodeTest004',

    # Maybe tested with lxml
    'fn-string__fn-string-30',  # parse of comments required
]


QT3_NAMESPACE = "http://www.w3.org/2010/09/qt-fots-catalog"

namespaces = {'': QT3_NAMESPACE}


@contextlib.contextmanager
def working_directory(dirpath):
    orig_wd = os.getcwd()
    os.chdir(dirpath)
    try:
        yield
    finally:
        os.chdir(orig_wd)


class ExecutionError(Exception):
    """Common class for W3C XPath tests execution script."""


class ParseError(ExecutionError):
    """Other error generated by XPath expression parsing and static evaluation."""


class EvaluateError(ExecutionError):
    """Other error generated by XPath token evaluation with dynamic context."""


class Schema(object):
    """Represents an XSD schema used in XML environment settings."""

    def __init__(self, elem):
        assert elem.tag == '{%s}schema' % QT3_NAMESPACE
        self.uri = elem.attrib.get('uri')
        self.file = elem.attrib.get('file')
        try:
            self.description = elem.find('description', namespaces).text
        except AttributeError:
            self.description = ''


class Source(object):
    """Represents a source file as used in XML environment settings."""

    def __init__(self, elem):
        assert elem.tag == '{%s}source' % QT3_NAMESPACE
        self.file = elem.attrib['file']
        self.role = elem.attrib.get('role', '')
        self.uri = elem.attrib.get('uri')
        try:
            self.description = elem.find('description', namespaces).text
        except AttributeError:
            self.description = ''

        try:
            self.xml = ElementTree.parse(self.file)
        except ElementTree.ParseError:
            self.xml = None

    def __repr__(self):
        return '%s(file=%r)' % (self.__class__.__name__, self.file)


class Environment(object):
    """The XML environment definition for a test case."""

    def __init__(self, elem):
        assert elem.tag == '{%s}environment' % QT3_NAMESPACE
        self.name = elem.get('name', 'anonymous')
        self.namespaces = {
            namespace.attrib['prefix']: namespace.attrib['uri']
            for namespace in elem.iterfind('namespace', namespaces)
        }

        child = elem.find('schema', namespaces)
        if child is not None:
            self.schema = Schema(child)
        else:
            self.schema = None

        self.sources = {}
        for child in elem.iterfind('source', namespaces):
            source = Source(child)
            self.sources[source.role] = source
            if source.role is None:
                print(ElementTree.tostring(child))

    def __repr__(self):
        return '%s(name=%r)' % (self.__class__.__name__, self.name)

    def __str__(self):
        children = []
        for prefix, uri in self.namespaces.items():
            children.append('<namespace prefix="{}" uri="{}"/>'.format(prefix, uri))
        if self.schema is not None:
            children.append('<schema uri="{}" file="{}"/>'.format(
                self.schema.uri or '', self.schema.file or ''
            ))
        for role, source in self.sources.items():
            children.append('<source role="{}" uri="{}" file="{}"/>'.format(
                role, source.uri or '', source.file
            ))
        return '<environment name="{}">\n   {}\n</environment>'.format(
            self.name, '\n   '.join(children)
        )


class TestSet(object):
    """
    Represents a test-set as read from the catalog file and the test-set XML file itself.

    :param elem: the XML Element that contains the test-set definitions.
    :param environments: the global environments.
    """
    def __init__(self, elem, environments=None):
        assert elem.tag == '{%s}test-set' % QT3_NAMESPACE
        self.name = elem.attrib['name']
        self.file = elem.attrib['file']
        self.environments = {} if environments is None else environments.copy()
        self.test_cases = []

        self.spec_dependencies = []
        self.feature_dependencies = []
        self.xsd_version = None

        full_path = os.path.abspath(self.file)
        directory = os.path.dirname(full_path)
        filename = os.path.basename(full_path)
        with working_directory(directory):
            xml_root = ElementTree.parse(filename).getroot()

            self.description = xml_root.find('description', namespaces).text

            for child in xml_root.findall('dependency', namespaces):
                dep_type = child.attrib['type']
                value = child.attrib['value']
                if dep_type == 'spec':
                    self.spec_dependencies.extend(value.split(' '))
                elif dep_type == 'feature':
                    self.feature_dependencies.append(value)
                elif dep_type == 'xsd-version':
                    self.xsd_version = value
                else:
                    print("unexpected dependency type %s for test-set %r" % (dep_type, self.name))

            for child in xml_root.findall('environment', namespaces):
                environment = Environment(child)
                self.environments[environment.name] = environment

            for child in xml_root.findall('test-case', namespaces):
                self.test_cases.append(TestCase(child, self))

    def __repr__(self):
        return '%s(name=%r)' % (self.__class__.__name__, self.name)


class TestCase(object):
    """
    Represents a test case as read from a test-set file.

    :param elem: the XML Element that contains the test-case definition.
    :param test_set: the test-set that the test-case belongs to.
    """
    # Single value dependencies
    calendar = None
    default_language = None
    format_integer_sequence = None
    language = None
    limits = None
    unicode_version = None
    unicode_normalization_form = None
    xml_version = None
    xsd_version = None

    def __init__(self, elem, test_set):
        assert elem.tag == '{%s}test-case' % QT3_NAMESPACE
        self.test_set = test_set
        self.name = test_set.name + "__" + elem.attrib['name']
        self.description = elem.find('description', namespaces).text
        self.test = elem.find('test', namespaces).text

        result_child = elem.find('result', namespaces).find("*")
        self.result = Result(result_child, test_case=self)

        self.environment_ref = None
        self.environment = None
        self.spec_dependencies = []
        self.feature_dependencies = []

        for child in elem.findall('dependency', namespaces):
            dep_type = child.attrib['type']
            value = child.attrib['value']
            if dep_type == 'spec':
                self.spec_dependencies.extend(value.split(' '))
            elif dep_type == 'feature':
                self.feature_dependencies.append(value)
            elif dep_type in {'calendar', 'default-language', 'format-integer-sequence',
                              'language', 'limits', 'xml-version', 'xsd-version',
                              'unicode-version', 'unicode-normalization-form'}:
                setattr(self, dep_type.replace('-', '_'), value)
            else:
                print("unexpected dependency type %s for test-case %r" % (dep_type, self.name))

        child = elem.find('environment', namespaces)
        if child is not None:
            if 'ref' in child.attrib:
                self.environment_ref = child.attrib['ref']
            else:
                self.environment = Environment(child)

    def __repr__(self):
        return '%s(name=%r)' % (self.__class__.__name__, self.name)

    def __str__(self):
        children = [
            '<description>{}</description>'.format(self.description),
            '<test>{}</test>'.format(self.test) if self.test else '</test>',
            '<result>\n   {}\n</result>'.format(self.result),
        ]
        if self.environment_ref:
            children.append('<environment ref="{}"/>'.format(self.environment_ref))

        return '<test-case name="{}" test_set_file="{}"/>\n   {}\n</test-case>'.format(
            self.name,
            self.test_set_file,
            '\n   '.join('\n'.join(children).split('\n')),
        )

    @property
    def test_set_file(self):
        return self.test_set.file

    def get_xpath_context(self):
        env_ref = self.environment_ref
        if env_ref:
            try:
                environment = self.test_set.environments[env_ref]
            except KeyError:
                msg = "Unknown environment %s in test case %s"
                raise ExecutionError(msg % (env_ref, self.name)) from None
        elif self.environment:
            environment = self.environment
        else:
            environment = None

        if environment is None:
            return XPathContext(root=ElementTree.XML("<empty/>"))

        if '.' in environment.sources:
            root = environment.sources['.'].xml
        else:
            root = ElementTree.XML("<empty/>")

        if any(k.startswith('$') for k in environment.sources):
            variable_values = {
                k[1:]: v.xml for k, v in environment.sources.items() if k.startswith('$')
            }
            return XPathContext(root=root, variable_values=variable_values)

        return XPathContext(root=root)

    def run(self, verbose=1):
        if verbose >= 4:
            print("")
            print(str(self))
        return self.result.validate(verbose)

    def run_xpath_test(self, verbose=1, may_fail=False):
        """
        Helper function to parse and evaluate tests with elementpath.

        If may_fail is true, raise the exception instead of printing and aborting
        """
        try:
            parser = XPath2Parser()
            root_node = parser.parse(self.test)
        except Exception as err:
            if not may_fail and verbose >= 2:
                print("\nTest case {!r}: {}".format(self.name, self.test))
                print("Unexpected parse error %r: %s" % (type(err), str(err)))

            if verbose >= 4:
                if may_fail:
                    print("\nExpected error parsing %r: %s" % (self.test, str(err)))
                    print(str(self))
                traceback.print_exc()

            if isinstance(err, ElementPathError):
                raise
            raise ParseError(err)

        context = self.get_xpath_context()
        try:
            result = root_node.evaluate(context)
        except Exception as err:
            if not may_fail and verbose >= 2:
                print("\nTest case {!r}: {}".format(self.name, self.test))
                print("Unexpected evaluation error %r: %s" % (type(err), str(err)))

            if verbose >= 4:
                if may_fail:
                    print("\nExpected error evaluating %r: %s" % (self.test, str(err)))
                    print(str(self))
                traceback.print_exc()

            if isinstance(err, ElementPathError):
                raise
            raise EvaluateError(err)

        if verbose >= 4:
            print("Result of evaluation: {!r}".format(result))
        return result


class Result(object):
    """
    Class for validating the result of a test case. Result instances can
    be nested for multiple validation options. There are several types
    of result validators available:

      * all-of
      * any-of
      * assert
      * assert-count
      * assert-deep-eq
      * assert-empty
      * assert-eq
      * assert-false
      * assert-permutation
      * assert-serialization-error
      * assert-string-value
      * assert-true
      * assert-type
      * assert-xml
      * error
      * not
      * serialization-matches

    :param elem: the XML Element that contains the test-case definition.
    :param test_case: the test-case that the result validator belongs to.
    """

    def __init__(self, elem, test_case):
        self.test_case = test_case
        self.type = elem.tag.split('}')[1]
        self.value = elem.text
        self.attrib = {k: v for k, v in elem.attrib.items()}
        self.children = [Result(child, test_case) for child in elem.findall('*')]
        self.validate = getattr(self, '%s_validator' % self.type.replace("-", "_"))

    def __repr__(self):
        return '%s(type=%r)' % (self.__class__.__name__, self.type)

    def __str__(self):
        attrib = ' '.join('{}="{}"'.format(k, v) for k, v in self.attrib.items())
        if self.children:
            return '<{0} {1}>{2}{3}\n</{0}>'.format(
                self.type,
                attrib,
                self.value if self.value is not None else '',
                '\n   '.join(str(child) for child in self.children),
            )
        elif self.value is not None:
            return '<{0} {1}>{2}</{0}>'.format(self.type, attrib, self.value)
        else:
            return '<{} {}/>'.format(self.type, attrib)

    def all_of_validator(self, verbose=1):
        """Valid if all child result validators are valid."""
        assert self.children
        result = True
        for child in self.children:
            if not child.validate(verbose):
                result = False
        return result

    def any_of_validator(self, verbose=1):
        """Valid if any child result validator is valid."""
        assert self.children
        result = False
        for child in self.children:
            if child.validate():
                result = True

        if not result and verbose > 1:
            for child in self.children:
                child.validate(verbose)
        return result

    def not_validator(self, verbose=1):
        """Valid if the child result validator is not valid."""
        assert len(self.children) == 1
        result = not self.children[0].validate()
        if not result and verbose > 1:
            self.children[0].validate(verbose)
        return result

    def assert_eq_validator(self, verbose=1):
        try:
            result = self.test_case.run_xpath_test(verbose)
        except (ElementPathError, ParseError, EvaluateError):
            return False

        if type(result) == list and len(result) == 1:
            result = result[0]

        parser = XPath2Parser()
        root_node = parser.parse(self.value)
        context = XPathContext(root=ElementTree.XML("<empty/>"))
        return root_node.evaluate(context) == result

    def assert_type_validator(self, verbose=1):
        try:
            result = self.test_case.run_xpath_test(verbose)
        except (ElementPathError, ParseError, EvaluateError):
            return False

        if self.value == 'xs:anyURI':
            return isinstance(result, str)
        elif self.value == 'xs:boolean':
            return isinstance(result, bool)
        elif self.value == 'xs:date':
            return isinstance(result, elementpath.datatypes.Date10)
        elif self.value == 'xs:double':
            return isinstance(result, float)
        elif self.value == 'xs:dateTime':
            return isinstance(result, elementpath.datatypes.DateTime10)
        elif self.value == 'xs:dayTimeDuration':
            return isinstance(result, elementpath.datatypes.Timezone)
        elif self.value == 'xs:decimal':
            return isinstance(result, decimal.Decimal)
        elif self.value == 'xs:float':
            return isinstance(result, float)
        elif self.value == 'xs:integer':
            return isinstance(result, int)
        elif self.value == 'xs:NCName':
            return isinstance(result, str)
        elif self.value == 'xs:nonNegativeInteger':
            return isinstance(result, int)
        elif self.value == 'xs:positiveInteger':
            return isinstance(result, int)
        elif self.value == 'xs:string':
            return isinstance(result, str)
        elif self.value == 'xs:time':
            return isinstance(result, elementpath.datatypes.Time)
        elif self.value == 'xs:token':
            return isinstance(result, str)
        elif self.value == 'xs:unsignedShort':
            return isinstance(result, int)
        elif self.value.startswith('document-node') or self.value.startswith('element'):
            return isinstance(result, list)
        else:
            msg = "unknown type in assert_type: %s (result type is %s), test-case %s"
            print(msg % (self.value, str(type(result)), self.test_case.name))
            sys.exit(1)

    def assert_string_value_validator(self, verbose=1):
        try:
            result = self.test_case.run_xpath_test(verbose)
        except (ElementPathError, ParseError, EvaluateError):
            return False
        else:
            if isinstance(result, float):
                if math.isnan(result):
                    return self.value == 'NaN'
                elif math.isinf(result):
                    return self.value.lower().startswith(str(result))
            return str(result) == self.value

    def error_validator(self, verbose=1):
        try:
            self.test_case.run_xpath_test(verbose, may_fail=True)
        except ElementPathError as err:
            if 'code' not in self.attrib:
                return True

            code = self.attrib['code'].strip()
            if code == '*' or code in str(err):
                return True
            if 3 <= verbose < 4:
                print("\n{} code not found in {!r}: {}".format(code, type(err), str(err)))
                if verbose == 3:
                    print("Test case {!r}: {}".format(self.test_case.name, self.test_case.test))
                else:
                    print(str(self.test_case))
            return False

        except (ParseError, EvaluateError) as err:
            if 2 <= verbose < 4:
                print("\nNot an elementpath error {!r}: {}".format(type(err), str(err)))
                if verbose == 2:
                    print("Test case {!r}: {}".format(self.test_case.name, self.test_case.test))
                else:
                    print(str(self.test_case))
            return self.attrib.get('code', '*') == '*'

        else:
            return False

    def assert_true_validator(self, verbose=1):
        """Valid if the result is `True`."""
        try:
            return self.test_case.run_xpath_test(verbose) is True
        except (ElementPathError, ParseError, EvaluateError):
            return False

    def assert_false_validator(self, verbose=1):
        """Valid if the result is `False`."""
        try:
            return self.test_case.run_xpath_test(verbose) is False
        except (ElementPathError, ParseError, EvaluateError):
            return False

    def assert_count_validator(self, verbose=1):
        """Valid if the number of items of the result matches."""
        try:
            result = self.test_case.run_xpath_test(verbose)
        except (ElementPathError, ParseError, EvaluateError):
            return False

        if type(result) == str:
            return int(self.value) == 1
        else:
            return int(self.value) == len(result)

    def assert_validator(self, verbose=1):
        """
        Assert validator contains an XPath expression whose value must be true.
        The expression may use the variable $result, which is the result of
        the original test.
        """
        try:
            result = self.test_case.run_xpath_test(verbose)
        except (ElementPathError, ParseError, EvaluateError):
            return False

        variables = {'result': result}
        parser = XPath2Parser(variables=variables)
        root_node = parser.parse(self.value)
        context = XPathContext(root=ElementTree.XML("<empty/>"), variable_values=variables)
        return root_node.evaluate(context) is True

    def assert_deep_eq_validator(self, verbose=1):
        try:
            result = self.test_case.run_xpath_test(verbose)
        except (ElementPathError, ParseError, EvaluateError):
            return False

        expression = "fn:deep-equal($result, (%s))" % self.value
        variables = {'result': result}

        parser = XPath2Parser(variables=variables)
        root_node = parser.parse(expression)
        context = XPathContext(root=ElementTree.XML("<empty/>"), variable_values=variables)
        return root_node.evaluate(context) is True

    def assert_empty_validator(self, verbose=1):
        """Valid if the result is empty."""
        try:
            result = self.test_case.run_xpath_test(verbose)
        except (ElementPathError, ParseError, EvaluateError):
            return False
        else:
            return result is None or result == []

    def assert_permutation_validator(self, verbose=1):
        """ TODO """

    def assert_serialization_error_validator(self, verbose=1):
        # TODO: this currently succeeds on any error
        try:
            self.test_case.run_xpath_test(verbose)
        except (ElementPathError, ParseError, EvaluateError):
            return True
        else:
            return False

    def assert_xml_validator(self, verbose=1):
        try:
            result = self.test_case.run_xpath_test(verbose)
        except (ElementPathError, ParseError, EvaluateError):
            return False

        if result is None:
            return False
        if type(result) == list:
            parts = []
            for item in result:
                if isinstance(item, elementpath.TextNode):
                    parts.append(str(item))
                else:
                    parts.append(ElementTree.tostring(item).decode('utf-8').strip())
            xml_str = "".join(parts)
        else:
            xml_str = ElementTree.tostring(result.getroot()).decode('utf-8').strip()

        if verbose >= 5:
            print("Final XML string to compare: '%s'" % xml_str)
        return xml_str == self.value

    def serialization_matches_validator(self, verbose=1):
        try:
            result = self.test_case.run_xpath_test(verbose)
        except (ElementPathError, ParseError, EvaluateError):
            return False

        regex = re.compile(self.value)
        return regex.match(result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('catalog', metavar='CATALOG_FILE',
                        help='the path to the main index file of test suite (catalog.xml)')
    parser.add_argument('pattern', nargs='?', default='', metavar='PATTERN',
                        help='run only test cases which name matches a regex pattern')
    parser.add_argument('-v', dest='verbose', action='count', default=1,
                        help='increase verbosity: one option to show unexpected errors, '
                             'two for show also unmatched error codes, three for debug')
    parser.add_argument('-r', dest='report', metavar='REPORT_FILE',
                        help="Write a report (JSON format) to the given file")
    args = parser.parse_args()

    report = OrderedDict()
    report["summary"] = OrderedDict()
    report['other_failures'] = []
    report['unknown'] = []
    report['failed'] = []
    report['success'] = []

    catalog_file = os.path.abspath(args.catalog)
    if not os.path.isfile(catalog_file):
        print("Error: catalog file %s does not exist" % args.catalog)
        sys.exit(1)

    with working_directory(dirpath=os.path.dirname(catalog_file)):
        catalog_xml = ElementTree.parse(catalog_file)

        environments = {}
        for child in catalog_xml.getroot().iterfind("environment", namespaces):
            environment = Environment(child)
            environments[environment.name] = environment

        test_sets = {}
        for child in catalog_xml.getroot().iterfind("test-set", namespaces):
            test_set = TestSet(child, environments)
            test_sets[test_set.name] = test_set

        count_read = 0
        count_skip = 0
        count_run = 0
        count_success = 0
        count_failed = 0
        count_unknown = 0
        count_other_failures = 0

        for test_set in test_sets.values():
            # ignore test cases for XQuery, and 3.0
            ignore_all_in_test_set = any(
                dep in IGNORE_DEPENDENCIES for dep in test_set.spec_dependencies
            )

            for test_case in test_set.test_cases:
                # If a pattern argument is provided runs only cases with matching name
                if args.pattern not in test_case.name:
                    continue

                count_read += 1
                if ignore_all_in_test_set:
                    count_skip += 1
                    continue

                # ignore test cases for XQuery, and 3.0
                if any(dep in IGNORE_DEPENDENCIES for dep in test_case.spec_dependencies):
                    count_skip += 1
                    continue

                # ignore tests that rely on higher-order function such as array:sort()
                if 'higherOrderFunctions' in test_case.feature_dependencies:
                    count_skip += 1
                    continue

                if test_case.name in SKIP_TESTS:
                    count_skip += 1
                    continue

                count_run += 1
                try:
                    case_result = test_case.run(verbose=args.verbose)
                    if case_result is True:
                        if args.report:
                            report['success'].append(test_case.name)
                        count_success += 1
                    elif case_result is False:
                        if args.report:
                            report['failed'].append(test_case.name)
                        count_failed += 1
                    else:
                        if args.report:
                            report['unknown'].append(test_case.name)
                        count_unknown += 1
                except Exception as err:
                    print("\nUnexpected failure for test %r" % test_case.name)
                    print(type(err), str(err))

                    if args.verbose >= 4:
                        traceback.print_exc()
                    if args.report:
                        report['other_failures'].append(test_case.name)
                    count_other_failures += 1

        print("\n*** Totals of W3C XPath tests execution ***\n")
        print("%d test cases read" % count_read)
        print("%d test cases skipped" % count_skip)
        print("%d test cases run\n" % count_run)
        print("  %d success" % count_success)
        print("  %d failed" % count_failed)
        print("  %d unknown" % count_unknown)
        print("  %d other failures" % count_other_failures)

        if args.report:
            report['summary']['read'] = count_read
            report['summary']['skipped'] = count_skip
            report['summary']['run'] = count_run
            report['summary']['success'] = count_success
            report['summary']['failed'] = count_failed
            report['summary']['unknown'] = count_unknown
            report['summary']['other_failures'] = count_other_failures
            with open(args.report, 'w') as outfile:
                outfile.write(json.dumps(report, indent=2))


if __name__ == '__main__':
    sys.exit(main())
