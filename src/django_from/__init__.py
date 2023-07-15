'''
Created on 14 Jul.,2023

@author: Bernd Wechner
@status: Alpha - experimental

Provides queryset encapsulation to get around Django's

    Cannot compute Aggreage('field'): 'field' is an aggregate

limitation. What Django currently lacks is queryset encapsulation, that
is, the ability to build a queryset upon an encapsulated queryset. Or,
in other words, put an existing querset in the FROM clause of the SQL.

Django's Queryset cannot select from Subqueries and builds queries through
successive Joins only. And while Djnago support SubQueries, only as
annotations (in teh SELECT clause) not as source (in the FROM clause).

Because Djangos QuerySest can be quite complicated this is a running
experiment. Tested on a particular use case and vfunctional but quite
likely to break on some fancy query or other ...
'''
import re

from sqlparse import parse, tokens
from sqlparse.sql import Identifier, IdentifierList

from django.db.models import Model, QuerySet
from django.db.models.sql import Query
from django.db.models.sql.compiler import SQLCompiler

from django.db.utils import ConnectionHandler
from django.db.models.expressions import Col, Value

def replace_nth(string, old, new, n):
    '''Replace the nth occurence of old in string with new.'''
    parts = string.split(old, n)
    return new.join(parts)

class FromMixIn():
    '''
    A model MixIn which provides the From class method. This takes a queryset as an argument,
    that is to encapsulated. Thus providing a very neat syntax for the encapsulation:

    my_query = MyModel.objects.filter().annotate()....

    myresult = MyModel.From(my_query).filter().annotate()....

    The importance of this is that the annotations in the second chain can perfrom aggregations on
    aggregations that wre in the first chain! To wit we can aggregate aggreagates which Django
    cannot do for us alas.

    The main task at hand are:

    1. Park the queryset to date on the query (under query._from_query)
    2. Return a new clean queryset to start a new chain.
    3. Ensure the new queryset is aware of annotations from the parked queryset.
        This is mildly tricky as Django of course is not savvy about the trick we're
        about to pull (replacing FROM table with FROM subquery). To achieve it we
        need to add them as annotations to the new query but only as placeholders
        as there is no Django way of referring to the subquery at this stage.
        The simplest approoach si carry the annotations over with a placeholder
        Value. These can be tidied up when we do the Subquery insertion in
        FromCompiler
    4. Ensure the new queryset returns all the model fields and annotations from
        the first chain. That is what the second chain expects.
    '''
    @classmethod
    def From(cls, queryset):
        # Start the queryset anew
        new_queryset = queryset.model._default_manager.all()
        # Let Django know we want touse our custome Query (which we need to attach out custom SQL compiler)
        new_queryset._query = FromQuery(queryset.model)
        # Pass the old query to FromCompiler (so it can do the encapsulation)
        # We pull a trick here with values(). By default this forces the SELECT to
        # select all the model fields. The encapsulated QuerySet may have used
        # values() - for example, to elicit GROUP BY behaviour from Django - but if
        # we're encapsulating htis as an annotated model query we need it to return the
        # all the model fields polus any annotations. And that is what .values()
        # achieves.
        new_queryset.query._from_query = queryset.values().query
        # Add placeholder values to the new queryset as annotations
        # so that they can be referenced down the chain.
        for annotation in queryset.query.annotations:
            new_queryset.query.add_annotation(Value(f"{annotation}_placeholder"), annotation)
        # return the new queryset down the chain
        return new_queryset

class FromCompiler(SQLCompiler):
    '''
    Django provides excellent Middleware support between the client and server
    at the HTML interface, but no such thing unfortunately between the ORM and
    database at the SQL interface.

    The SQLcompiler is reponsible for generating SQL and so we derive from
    SQL compiler and override as_sql, get the original SQL and then doctor
    that. That is short ans sweet and saves us the trouble fo implementing
    a SQL compiler for what is a small change to the existing SQL generation.

    The encapsulate Query is provided in self.query._from_query and the task
    here is to:

    1. Replace the models "FROM model_table" with "FROM (_from_query) model_table"
        So we're now SELECTing from the _from_query posting as the original table
    2. Remove the placeholder annotations that we added in From() when parking the
        _from queryset. These were needed to atht references to those fields in any
        annotations following From() satisdy Django's checks for valid references.
        But they were just placeholders as they have no meaning in the wrapping
        query.
    2. Clean up the compiler
        Alas, this is empirical, not document by Django. oTo wit, based on study
        of the code and experiments. But at the very least these compiler attributes
        need updating so that the SQL can be executed successfully:

        self.select                which is a list of the items following the SELECT
        self.annotation_col_map    which is a dictionary of the annotations and their index into self.select
        self.col_count             which is a count of the items in self.select
    '''
    def as_sql(self, with_limits=True, with_col_aliases=False):
        super_sql, super_params = super().as_sql(with_limits, with_col_aliases)
        sql = super_sql
        params = list(super_params)

        # some RE flags we'll use
        flags = re.RegexFlag.IGNORECASE & re.RegexFlag.MULTILINE & re.RegexFlag.DOTALL

        # Correct Django smarts. Alas in:
        #     django.db.models.sql.compiler.SQLCompiler.collapse_group_by
        # Django pulls a sly trick collapsing the GROUP BY down to just the
        # ID.Some databases (postgresql in particular, but not SQLlite) permit
        # this, because well, it's cool and terse, as all the other model fields
        # are dependent on that primary key.
        #
        # Their comment:
        #     If the database supports group by functional dependence reduction,
        #     then the expressions can be reduced to the set of selected table
        #     primary keys as all other columns are functionally dependent on them.
        #
        # Alas, if we're going to select from a subquery that group by reduction won't
        # work as the subquery lacks a primary key. Doh! So we have to go put them all
        # back again.
        #
        # If there's a GROUP BY, we want to find all the selects that are not aggregates
        # and ensure they are all in the GROUP BY.
        #
        # self.select has 3-tuples of form (col, (sql, params), alias)
        #
        # first check if we have a GROUP BY and what's in it
        psql = parse(sql)[0].tokens
        i = 0
        while i < len(psql) and not (psql[i].is_keyword and psql[i].value == 'GROUP BY'): i += 1
        if i < len(psql):
            # We have a GROUP BY
            while i < len(psql) and not (isinstance(psql[i], (Identifier, IdentifierList))): i += 1
            group_by = re.split(r'\s*,\s*', psql[i].value)
            re_group_by = fr'(GROUP\s+BY\s+)({psql[i].value})'

            cols_to_add = []
            for col, sql_parameters, alias in self.select:
                ref = alias if alias else sql_parameters[0] % sql_parameters[1]
                if isinstance(col, Col) and not ref in group_by:
                    cols_to_add.append(ref)

            # Find the GROUP BY and fix it
            if cols_to_add:
                col_list = ", ".join(cols_to_add)
                sql = re.sub(re_group_by, fr'\g<0>,{col_list}', sql)

        # Capture the subquery that From() wants us to encapsulate
        from_query = self.query._from_query
        from_sql, from_params = from_query.sql_with_params()

        table = self.query.model._meta.db_table

        # Remove the annotation selections (they were annotations on the
        # encapsulated From query and are no longer needed, but we needed
        # to preserve them as annotations of a sort (with a dummy value)
        # in the queryset chain so that the default SQlcompiler is apply
        # with references to them (it wants all references to be to the
        # model fields or annotations and bails if we make a reference to
        # something it believs does not exist - rightly, as it's unaware of
        # the subquery we're about to patch in.
        # Copy the annotation field definitions
        for a in from_query.annotations:
            # They apepar in the SWL as
            #    %s AS "a",
            # between the SELECT and FROM
            before = r'^(\s*SELECT\s+.*?)'
            placeholder_annotation = fr'(,?\s*%s\s+AS\s+"?{a}"?)'
            after = r'([,\s].*\s+FROM\s+.*)$'
            pattern = fr'{before}{placeholder_annotation}{after}'
            if matches:=re.match(pattern, sql, flags):
                sql = re.sub(pattern, r"\1\3", sql, flags)
                param_select = matches.group(2)
                param_index = matches.group(1).count("%s")
                del params[param_index]

                # We also need to remove it from the annotation cols map or Django will
                # think  it's still there when it goes to execute the SQL.
                if a in self.annotation_col_map:
                    select_index = self.annotation_col_map[a]
                    # Remove it from the select list
                    del self.select[select_index]

                    # Remove it from the annotation cols map
                    del self.annotation_col_map[a]

                    # All items in the annotation_col_map higher than this one have to drop on
                    for A,C in self.annotation_col_map.items():
                        if C > select_index:
                            self.annotation_col_map[A] -= 1

                    # Decrement the column count
                    self.col_count -= 1

                    # The placeholder(s) we put it in encoded are encoded parameters.
                    # We  want substitute them in now.
                    placeholder = f"{a}_placeholder"
                    while placeholder in params:
                        params_index = params.index(placeholder)
                        del params[params_index]
                        sql = replace_nth(sql, '%s', f'"{a}"', params_index+1)
                else:
                    raise ValueError(f'Epected annotation "{a}" in Django annotation_col_map and was disappointed.')
            else:
                raise ValueError(f'Expected annotation "{a}" in SQL and was disappointed.')

        # Now replace the model table with the subquery that From() encapsulated
        # We give it the alias name of the table itself so that all the
        # SELECT, GROUP BY, WHERE or other field references function as expected.
        # It's a tad unusual to give the subquery an alais the same as a table
        # name but SQl is happy with it (different scope and we are in fact
        # encapsulating a query on that table that has annotations)
        before = r'^(.*?FROM\s+)'
        table_name = f'("?{table}"?)'
        after = r'(.*)$'
        pattern = fr'{before}{table_name}{after}'
        if matches:=re.match(pattern, sql, flags):
            sql = re.sub(pattern, fr'\1({from_sql}) "{table}" \3', sql, flags)

            # Now we need to graft the from_params into the params.
            if from_params:
                # Count how many params are before those in our grafted SQL
                before = matches.group(1).count('%s')
                params.insert(before+1, *from_params)
        else:
            raise ValueError(f'Expected table"{table}" in SQL and was disappointed.')

        # Make params immutable again.
        params = tuple(params)

        # print(super_sql % super_params)
        # print(from_sql % from_params)
        # print(sql % params)
        return sql, params

class FromQuery(Query):
    '''
    It is the Query object that supples the SQL compiler, and so we need to override it
    to return our custom SQL compiler. A litle of the logic from the get_comiler method
    we are overriding has to be duplicated as there is no way around that.
    '''
    def get_compiler(self, using=None, connection=None, elide_empty=True):
        if using is None and connection is None:
            raise ValueError("Need either using or connection")
        if using:
            connections = ConnectionHandler()
            connection = connections[using]
        return FromCompiler(self, connection, using, elide_empty)

