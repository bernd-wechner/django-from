'''
Created on 14 Jul.,2023

@author: Bernd Wechner
@status: Alpha - experimental

Provides queryset encapsulation to get around Django's

    Cannot compute Aggregate('field'): 'field' is an aggregate

limitation. What Django currently lacks is queryset encapsulation, that
is, the ability to build a queryset upon an encapsulated queryset. Or,
in other words, put an existing queryset in the FROM clause of the SQL.

Django's Queryset cannot select from Subqueries and builds queries through
successive Joins only. And while Django supports Subqueries, it only supports 
them as annotations (in the SELECT clause) not as a source (in the FROM clause).

Because Django's QuerySest can be quite complicated this is a running
experiment. Tested on a particular use case and functional but quite
likely to break on some fancy query or other ...
'''
import re

from datetime import datetime, timedelta

from sqlparse import parse, tokens, format as SQLformat
from sqlparse.sql import Identifier, IdentifierList

from django.db import DEFAULT_DB_ALIAS
from django.db.models import Model, QuerySet
from django.db.models.sql import Query
from django.db.models.sql.compiler import SQLCompiler

from django.db.utils import ConnectionHandler
from django.db.models.expressions import Col, Value

def is_literal(value):
    return isinstance(value, (int, float, str, bool))

import sys, os, threading, traceback
def print_diagnostics(message, n=5):
    return # Disable
    print("======================= START Stack Diagnostics ===============================")
    print(f"Message: {message}")
    print(f"Process ID: {os.getpid()}")
    print(f"Thread ID: {threading.get_ident()}")
    print("Top {} Stack Frames:".format(n))
    frame = sys._getframe()
    for i in range(n):
        if frame is None:
            break
        print(f"  Frame {i+1}: {frame.f_code.co_filename}, line {frame.f_lineno}, in {frame.f_code.co_name}")
        frame = frame.f_back
    print("========================= END Stack Diagnostics ===============================")

def executeable_SQL(SQL, params):
    '''
    Takes Django queryset SQL and aprameters tuple and produces executeable SQL for testing.
    
    Needed because datetimes and timedeltas need formatting to SQL norms. 
    
    :param SQL: a SQL string
    :param params: A tuple of parameters
    '''
    # Simple quality assurance - this premise should never break 
    n_slots = SQL.count("%s")
    n_vals = len(params)
    assert n_slots==n_vals

    # params is a dict of parameters.
    # DateTimes and TimeDeltas are alas converted to strings without the requiste
    # wrapping in single quotes. So we replace them by strign reps wrapped in single
    # quotes
    params = list(params)
    for i, p in enumerate(params):
        if isinstance(p, datetime):
            params[i] = "'" + str(p) + "'"
        elif isinstance(p, timedelta):
            params[i] = "INTERVAL '" + str(p) + "'"
    params = tuple(params)

    # And this is used when excuted as described here:
    #  https://docs.djangoproject.com/en/2.2/topics/db/sql/#passing-parameters-into-raw
    #
    # The key note being:
    #     params is a list or dictionary of parameters.
    #     You’ll use %s placeholders in the query string for a list,
    #     or %(key)s placeholders for a dictionary (where key is replaced
    #     by a dictionary key, of course)
    #
    # Which is precisely how Python2 standard % formating works.
    return SQL % params

class FromMixIn():
    '''
    A model MixIn which provides the From class method. This takes a queryset as an argument,
    that is to encapsulated. Thus providing a very neat syntax for the encapsulation:

    my_query = MyModel.objects.filter().annotate()....

    myresult = MyModel.From(my_query).filter().annotate()....

    The importance of this is that the annotations in the second chain can perform aggregations on
    aggregations that were in the first chain! To wit we can aggregate aggregates which Django
    cannot do for us alas.

    The main tasks at hand are:

    1. Park the queryset to date on the query object (under query._from_query)
    2. Return a new clean queryset to start a new chain.
    3. Ensure the new queryset is aware of annotations from the parked queryset.
        This is mildly tricky as Django of course is not savvy about the trick we're
        about to pull (replacing "FROM table" with "FROM subquery"). To achieve it we
        need to add them as annotations to the new query but only as placeholders
        as there is no Django way of referring to the subquery at this stage.
        The simplest approach is carry the annotations over with a placeholder
        value. These can be tidied up when we do the Subquery insertion in
        FromCompiler
    4. Ensure the new queryset returns all the model fields and annotations from
        the first chain. That is what the second chain expects.
    '''
    @classmethod
    def From(cls, queryset, debug=False):
        # Check if the queryset already has a From query!
        from_query = getattr(queryset.query, "_from_query", None)

        # Get the inner select (early, it's not generally available until the SQL is being generated)
        default_compiler = queryset.query.get_compiler(DEFAULT_DB_ALIAS)
        default_compiler.setup_query(with_col_aliases=True)
        inner_select=default_compiler.select

        # Start the queryset anew
        new_queryset = queryset.model._default_manager.all()
        # Let Django know we want to use our custom Query (which we need to do to attach our custom SQL compiler)
        new_queryset._query = FromQuery(queryset.model)
        # Pass the old query to FromCompiler (so it can do the encapsulation)
        # We pull a trick here with values(). By default this forces the SELECT to
        # select all the model fields. The encapsulated QuerySet may have used
        # values() - for example, to elicit GROUP BY behaviour from Django - but if
        # we're encapsulating this as an annotated model query we need it to return the
        # all the model fields plus any annotations. And that is what .values()
        # achieves.
        new_queryset.query._from_query = queryset.values().query  # This may be carrying a _from_query with it!
        # Add placeholder values to the new queryset as annotations
        # so that they can be referenced down the chain.
        for annotation in queryset.query.annotations:
            new_queryset.query.add_annotation(Value(f"{annotation}_placeholder"), annotation)
        # Attach a debug flag so the complier can emit debugging info if desired
        new_queryset.query.debugFrom = debug

        if debug:
            print(f"\n\n======================= START From ============================================")
            print_diagnostics("In From()")

        # Attach the inner select to the inner query
        new_queryset.query._from_query.compiler_select = inner_select

        if debug:
            if from_query:
                if isinstance(from_query, FromQuery):
                    inner_sql, inner_params = from_query.get_compiler(DEFAULT_DB_ALIAS, use_default_compiler=True).as_sql()
                else:
                    inner_sql, inner_params = from_query.get_compiler(DEFAULT_DB_ALIAS).as_sql()
                n_inner_slots =  inner_sql.count("%s")
                n_inner_vals = len(inner_params)
            else:
                inner_sql, inner_params = queryset.query.get_compiler(DEFAULT_DB_ALIAS).as_sql()
                n_inner_slots =  inner_sql.count("%s")
                n_inner_vals = len(inner_params)

            outer_sql, outer_params = new_queryset.query.get_compiler(DEFAULT_DB_ALIAS, use_default_compiler=True).as_sql()
            n_outer_slots = outer_sql.count("%s")
            n_outer_vals = len(outer_params)

            print("=============================DEBUG From =======================================")
            if from_query: print(f"\tFROM another query!")
            print(f"\n======== Inner SQL ({n_inner_slots} parameters):\n{SQLformat(inner_sql, reindent=True, keyword_case='upper')}")
            print(f"\n======== Inner Executable SQL:\n{SQLformat(executeable_SQL(inner_sql,inner_params), reindent=True, keyword_case='upper')}")
            print(f"\n======== Default Outer SQL ({n_outer_slots} parameters):\n{SQLformat(outer_sql, reindent=True, keyword_case='upper')}")
            print(f"\n======== Default Outer Executable SQL:\n{SQLformat(executeable_SQL(outer_sql, outer_params), reindent=True, keyword_case='upper')}")
            print(f"======================= END From ==============================================\n\n")

        # return the new queryset down the chain
        return new_queryset

class SQLFromCompiler(SQLCompiler):
    '''
    Django provides excellent Middleware support between the client and server
    at the HTML interface, but no such thing unfortunately between the ORM and
    database at the SQL interface.

    The SQLcompiler is responsible for generating SQL and so we derive from
    SQL compiler and override as_sql, get the original SQL and then doctor
    that. That is short and sweet and saves us the trouble of (re)implementing
    a SQL compiler for what is a small change to the existing SQL generation.

    The encapsulated Query is provided in self.query._from_query and the task
    here is to:

    1. Replace the models "FROM model_table" with "FROM (_from_query) model_table"
        So we're now SELECTing from the _from_query posing as the original table
        
    2. Remove the placeholder annotations that we added in From() when parking the
        _from queryset. These were needed so that references to those fields in any
        annotations following From() satisfy Django's checks for valid references.
        But they were just placeholders as they have no meaning in the wrapping
        query.
        
    2. Clean up the compiler
        Alas, this is empirical, not document by Django. To wit, based on study
        of the code and experiments. But at the very least these compiler attributes
        need updating so that the SQL can be executed successfully:

        WARNING. there are two distinct select atributes:
                Query.select:    which is a list of objects (one for each selected column)
                Compiler.select  which is a list of 3-tuples contains the object and a 
                                 generated (SQL, params) tuple and an alias.
                                 
        The Compiler builds its self.select when self.setup_query(with_col_aliases=True)
        is called from its self.query.
        
        So we want to get inner and outer selected tuple lists with care.
        
        

        self.select                which is a list of the items following the SELECT
        self.annotation_col_map    which is a dictionary of the annotations and their index into self.select
        self.col_count             which is a count of the items in self.select
        
    This makes us very dependent upon undocumented Django internals alas. 
    '''
    def add_to_GROUP_BY(self, sql, cols):
        '''
        A DRY method to add a list of cols to the GROUP in a SQL string. Need it in two contexts in as_sql, so ...
        :param sql: A string containing SQL
        :param cols: a string or list of strings being columns to add.
        '''
        re_group_by = fr'(GROUP\s+BY\s+)(.+?)$'
        col_list = ", ".join(cols) if isinstance(cols, (list, tuple)) else cols
        return re.sub(re_group_by, fr'\g<0>, {col_list}', sql, self.reflags)

    def as_sql(self, with_limits=True, with_col_aliases=False):
        '''
        Modifies the SQL generated by the default SQL compiler to encapsulate query stored 
        in self.query._from_query as the FROM we select this query from.
        
        We need to observe these properties of a Query:
        
        self.cols          a count of columns selected
        self.select        a list of output columns (should be self.cols in number by default but could be less)
        self.default_cols  a bool, which is true by default but the values() method on the QuerySet indirectly drops it to false when self.select is set..
        self.annotations   a list of the annotations (each is a column in self.select) applied by the Queryse annotate() method
        
        :param with_limits:
        :param with_col_aliases:
        '''
        try:
            debug = getattr(self.query, "debugFrom", False)
    
            if debug:
                print(f"\n\n============================= START as_sql ====================================")
                print_diagnostics("In as_sql()")

            # The default SQL compiler is at:
            #     django.db.models.sql.compiler.SQLCompiler.as_sql()
            # known here as super().as_sql()
            # We can it to get the SQL Django would supply by default.
            # This is the basis Outer query that we need to build by replacing 
            # From whatever with From the Inner query. 
            super_sql, super_params = super().as_sql(with_limits, with_col_aliases)
            sql = super_sql
            params = list(super_params)
    
            # The SQL should contain one %s field per param in super_params. Assert this
            # as it is the understanding of what super() (the standard SQL compiler for 
            # querysets) provides
            assert isinstance(super_sql, str)
            n_super_slots = super_sql.count("%s")
            n_super_vals = len(super_params)
            assert n_super_slots==n_super_vals

            # Capture the subquery that From() wants us to encapsulate (the Inner query)
            from_query = getattr(self.query, "_from_query")

            if from_query:
                # Get the nested levels:
                levels = 1
                last_from = from_query
                while getattr(last_from, "_from_query", False):
                    last_from = last_from._from_query 
                    levels += 1

                # Clean the SELECT Outputs
                # The Outer Query might be applying some methods that alter self.select. 
                # Notably, the the QuerySet.values() method is one such method of selecting 
                # which columns are going be selected (and hence appear in self.select)
                outer_selected = self.select
                inner_selected = list(from_query.select)  # We may need to modify this
                inner_annotations_selected = from_query.annotation_select  # We may need to modify this
    
                if debug:
                    print(f"\tnested levels: {levels}")
                    print(f"\touter_selected has {len(outer_selected)} entries:")
                    for i, s in enumerate(outer_selected): 
                        print(f"\t\tEntry {i}")
                        print(f"\t\t\tObject: {s[0]}")
                        print(f"\t\t\tSQL: {s[1][0]}")
                        print(f"\t\t\tParams:")
                        for p in s[1][1]:
                            print(f"\t\t\t\t{p}")
                        print(f"\t\t\tAlias: {s[2]}")
                    print(f"\tinner_selected has {len(inner_selected)} entries:")
                    for s in inner_selected: print(f"\t\t{s}")
                    print(f"\tinner_annotations_selected has {len(inner_annotations_selected)} entries:")
                    for s in inner_annotations_selected: print(f"\t\t{s}: {inner_annotations_selected[s]}")
    
                # The  outer selected columns all have the sql and params in them and the 
                # params will contain the placeholders for colmns in the inner query! So 
                # we can collect the referenced inner columns. THis is crucial if the outer 
                # query contains any aggregates
                #
                # DEPENDANCY: This depends on the format self.select which is taken from:
                #      https://github.com/django/django/blob/39b144baddca433b9aa28f99e595ffcc191c0bee/django/db/models/sql/compiler.py#L804
                # But is clearly dependent upon Django's implementation of self.select (likely rather stable)
                outer_performs_aggregation = False
                outer_references = set()
                for outer_column, (outer_column_sql, outer_column_params), outer_column_alias in outer_selected:
                    # Django does not set contains_aggregate to True when using Window functions but they are
                    # aggregators, and Django sets contains_over_clause to True. So we conbclude there is 
                    # aggregation happening if either is true.
                    if outer_column.contains_aggregate or outer_column.contains_over_clause: outer_performs_aggregation = True 
                    for param in outer_column_params:
                        if param.endswith("_placeholder"):
                            inner_column = param.removesuffix("_placeholder")
                            outer_references.add(inner_column)
            
                if debug:
                    print(f"\t{outer_performs_aggregation=}")
                    print(f"\t{outer_references=}")
    
                # If there's any aggregate columns then any inner selected items not referenced 
                # will cause an error. They must either not be supplied by the inner query (removed 
                # from the select) or added to the group-by.
                #
                # TODO: WE do GROUP_BY correction below. We should just add them all the the GROUP BY
                #       So don't need this. 
                # if outer_performs_aggregation:
                #     outer_group_by = self.query.group_by
                #
                #     for inner_index, inner_col in reversed(list(enumerate(inner_selected))):
                #         inner_column_alias = inner_col.alias
                #         if inner_column_alias not in outer_references and (not outer_group_by or inner_column_alias not in outer_group_by):
                #             del inner_selected[inner_index]
                #
                #     # TODO: inner_annotations_selected also needs to be trimmed of any not selected in the outer query. 
    
                from_sql, from_params = from_query.sql_with_params()
    
                # The SQL should contain one %s field per param in super_params. Assert this
                # as it is the understanding of what super() (the standard SQL compiler for 
                # querysets) provides
                assert isinstance(from_sql, str)
                n_from_slots = from_sql.count("%s")
                n_from_vals = len(from_params)
                assert n_from_slots==n_from_vals
        
                if debug:
                    #print("=============================================================================")
                    #traceback.print_tb()
                    print("========================== as_sql: INPUTS ===================================")
                    print(f"\n======== Inner SQL ({n_from_slots} parameters):\n{SQLformat(from_sql, reindent=True, keyword_case='upper')}")
                    print(f"\n======== Inner Executable SQL:\n{SQLformat(executeable_SQL(from_sql,from_params), reindent=True, keyword_case='upper')}")
                    print(f"\n======== Outer SQL ({n_super_slots} parameters):\n{SQLformat(sql, reindent=True, keyword_case='upper')}")
                    print(f"\n======== Outer Executable SQL:\n{SQLformat(executeable_SQL(sql, params), reindent=True, keyword_case='upper')}")
    
                
                # The %s placeholders can appear in the SQL clean selections:
                # like:
                #    %s AS "field_name"
                # or in aggregates like (but not limited to, and this can get complicated):
                #    SUM(%s) AS "sum_field_name"
                
                
                
                # The params are all placeholders put there by the From() class method which attached 
                # this compiler to the queryset. It parked the original queryset at: 
                #     self.query._from_query 
    
                # some RE reflags we'll use
                self.reflags = re.RegexFlag.IGNORECASE & re.RegexFlag.MULTILINE & re.RegexFlag.DOTALL
    
                # GROUP_BY expansion from default reduction!
                #
                # We need to correct Django smarts that apply GROUP_BY reduction. 
                # Alas in:
                #     django.db.models.sql.compiler.SQLCompiler.collapse_group_by
                # Django pulls a sly trick collapsing the GROUP BY down to just the ID.
                # Some databases (postgresql in particular, but not SQLlite) permit
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
                if debug: print(f"\nGROUP BY correction ========================================================")
    
                # TODO: If there are any over clauses we need to add a GROUP BY perhaps!
    
                psql = parse(sql)[0].tokens
                i = 0
                while i < len(psql) and not (psql[i].is_keyword and psql[i].value == 'GROUP BY'): i += 1
                outer_group_by_exists = i < len(psql)
                if outer_group_by_exists:
                    # We have a GROUP BY
                    while i < len(psql) and not (isinstance(psql[i], (Identifier, IdentifierList))): i += 1
                    group_by = re.split(r'\s*,\s*', psql[i].value)
    
                    cols_to_add = []
                    for col, sql_parameters, alias in self.select:
                        ref = alias if alias else sql_parameters[0] % sql_parameters[1]
                        if isinstance(col, Col) and not ref in group_by:
                            cols_to_add.append(ref)
    
                    if debug: print(f"\t\tAdding these columns to the GROUP BY: {cols_to_add}")
    
                    sql = self.add_to_GROUP_BY(sql, cols_to_add)
                else:
                    if debug: print(f"\t\tNot needed (no GROUP BY in the Outer query")
                    
    
                # The name of the table we're selecting from (in the SQL and FROM "table")
                table = self.query.model._meta.db_table
    
                # Annotation Correction:
                #
                # Inner annotations (annotations on query set From() is applied to)
                # all appear in the SQL as:
                #    %s AS "inner_annotation",
                # in the inner SQL.
                #
                # Remove the annotation selections (they were annotations on the
                # encapsulated From query and are no longer needed, but we needed
                # to preserve them as annotations of a sort (with a dummy value)
                # in the queryset chain so that the default SQlcompiler is applied
                # with references to them (it wants all references to be to the
                # model fields or annotations and bails if we make a reference to
                # something it believes does not exist - rightly, as it's unaware of
                # the subquery we're about to patch in.
                #
                # Copy the annotation field definitions
                outer_values={}
                for annotation, value in self.query.annotations.items():
                    if isinstance(value, Value):
                        outer_values[annotation] = value
        
                outer_aggregates = {}
                for annotation, value in self.query.annotations.items():
                    if value.contains_aggregate or value.contains_over_clause:
                        outer_aggregates[annotation] = value
        
                if debug: 
                    if debug: 
                        print(f"\nAnnotation correction ========================================================")
                        print(f"\t{len(from_query.annotations)} Inner annotations")
                        for annotation, value in from_query.annotations.items():
                            print(f"\t\t{annotation} = {value}")
                        print(f"\t{len(self.query.annotations)} Outer annotations")
                        for annotation, value in self.query.annotations.items():
                            print(f"\t\t{annotation} = {value}")
    
                        if outer_values: 
                            print(f"\t{len(outer_values)} Outer values")
                            for annotation, value in outer_values.items():
                                if value.value.endswith("_placeholder"):
                                    print(f"\t\tPlaceholder for {value.value.removesuffix('_placeholder')}")
                                else:
                                    print(f"\t\tNot a placeholder! (value={value.value})")
                        else:
                            print(f"\tNo Outer values")
    
                        if outer_aggregates: 
                            print(f"\t{len(outer_aggregates)} Outer aggregates")
                            for annotation, value in outer_aggregates.items():
                                print(f"\t\t{annotation} = {value}")
                        else:
                            print(f"\tNo Outer aggregates")
                            
                        if params:
                            print(f"\t{len(params)} parameters")
                            for param in params:
                                print(f"\t\t{param}")
                        else:
                            print(f"\tNo parameters")
    
                        print(f"\t{sql.count("%s")} parameter slots in SQL")
    
                # If there are any other kinds of annotations they are not yet supported and we 
                # want to consider them once encountered. 
                assert len(outer_values) + len(outer_aggregates) == len(self.query.annotations) 
    
                # Now in the Outer SQL we want to remove the params that are placeholder values and make them literals.
                # By which we mean replace '%s AS "annotation"' with '"annotation"' and remove it from params. These are
                # by their nature because not annotations anymore by columns ftom the inner SQL result and so can be
                # rendered as such.
                if debug: print(f"Fixing {len(outer_values)} parameter simple value substitutions in the Outer query:")
                for annotation, value in outer_values.items():
                    # Tricky we need to do this in order.params is a list
                    # in the order of the incidence of %s in the query. 
                    # So the mapping is in order. We need to find the 
                    # index of anything we're removing (how many %s before 
                    # it and how many after it for rigour and replace it in 
                    # the SQL and remove the item from the list)
                    #
                    # We may need to remove it from self.query.annotations too.  
                    if annotation == value.value.removesuffix('_placeholder'):
                        placeholder = f'%s AS "{annotation}"'
                        matches = re.findall(placeholder, sql)
    
                        if len(matches) == 1:
                            assert matches[0] == placeholder
    
                            before = sql[:sql.index(placeholder)].count('%s')
                            after = sql[sql.index(placeholder)+len(placeholder):].count('%s')
    
                            assert before + after + 1 == len(params), f"Counting error {before=}, {after=}, {len(params)=}"
    
                            # Remove the placeholder cleanly from the query and its SQL
                            sql = sql.replace(placeholder, f'"{annotation}"')
                            assert params[before] == value.value
                            del params[before]
                            
                            # self.query.annotations: contains all the annotations that were in the inner query
                            del self.query.annotations[annotation]
                            # self.annotation_col_map: contains only the annotations the outer query makes
                            del self.annotation_col_map[annotation]
                            if debug: print(f"\tReplaced placeholder for {annotation}, removed it from params ({len(params)} left) and annotations ({len(self.query.annotations)} left, {len(self.annotation_col_map)} in the col_map)")
                        elif len(matches) == 0:
                            # self.query.annotations: contains all the annotations that were in the inner query
                            # If we're not selecting it in the outer query we don't need it in the list of annotations
                            del self.query.annotations[annotation]
                            if debug: print(f"\t{annotation} was not referenced in the outer SQL.")
                        else:
                            raise ValueError(f"Unexpected scenario. {len(matches)} matches in the SQL for {annotation}")
    
                if debug: print(f"Fixing {len(outer_aggregates)} parameter aggregate substitutions in the Outer query:")
    
                # Other   
                new_params = []
                for param in params:
                    if isinstance(param, str) and param.endswith("_placeholder"):
                        val = param.removesuffix("_placeholder")
                        # params are in order of the %s fields, and so we replace only one instace, 
                        # the first instance and rmeain in lock step.  
                        sql = sql.replace("%s", f'"{val}"', 1)
                        # This should not appear as annotation because it's inside an aggregate annotation
                        if val in self.query.annotations:
                            raise ValueError(f"Unexpected scenario: {val} is in {self.query.annotations=}")
                        if debug: print(f"\tfixed {val}") 
                    else:
                        new_params.append(param)
                        if debug: print(f"\tNon placeholder parameter ({param}) conserved")
                
                params = new_params
    
                if debug:
                    n_slots = sql.count("%s")
                    n_vals = len(params)
                    assert n_slots==n_vals
                    
                    print(f"New Outer SQL ({n_slots} parameters):\n{SQLformat(sql, reindent=True, keyword_case='upper')}")
                    print(f"{len(params)} remaining params:")
                    for param in params:
                        print(f"\t{param}")
                    print(f"{len(params)} remaining annotations:")
                    for anno, val in self.query.annotations.items():
                        print(f"\t{anno} = {val}")
                
                # TABLE correction:
                #
                # Now replace the model table with the subquery that From() encapsulated
                # We give it the alias name of the table itself so that all the
                # SELECT, GROUP BY, WHERE or other field references function as expected.
                # It's a tad unusual to give the subquery an alias the same as a table
                # name but SQL is happy with it (different scope and we are in fact
                # encapsulating a query on that table that has annotations)
                before = r'^(.*?FROM\s+)'
                table_name = f'("?{table}"?)'
                after = r'(.*)$'
                pattern = fr'{before}{table_name}{after}'
                if matches:=re.match(pattern, sql, self.reflags):
                    if debug: print(f"Inserting Inner Query after FROM")
                    sql = re.sub(pattern, fr'\1({from_sql}) "{table}" \3', sql, self.reflags)
        
                    # Now we need to graft the from_params onto the params.
                    if from_params:
                        # Count how many params are before those in our grafted SQL
                        before = matches.group(1).count('%s')
                        params[before+1:before+1] = from_params
                else:
                    raise ValueError(f'Expected table"{table}" in SQL and was disappointed.')
                    
                # Make params immutable again.
                params = tuple(params)
        
                if debug:
                    n_slots = sql.count("%s")
                    n_vals = len(params)
                    assert n_slots==n_vals
                    print(f"Final SQL ({n_slots} parameters):\n{SQLformat(sql, reindent=True, keyword_case='upper')}")
                    print(f"{len(params)} remaining params:")
                    for param in params:
                        print(f"\t{param}")
                    print(f"{len(params)} remaining annotations:")
                    for anno, val in self.query.annotations.items():
                        print(f"\t{anno} = {val}")
        
                pg_sql_debug = False
                if pg_sql_debug:
                    import psycopg2
                    cp = self.connection.get_connection_params()
                    dsn = psycopg2.extensions.make_dsn(dbname=cp['dbname'], host=cp['host'], port=cp['port'], user=cp['user'], password=cp['password'])
                    with psycopg2.connect(dsn) as conn:
                        with conn.cursor() as curs:
                            print(sql % params)
                            curs.execute(sql % params)
                            for record in curs:
                                print(record)
    
                if debug:
                    print("========================== as_sql: OUTPUTS ==================================")
                    n_slots = sql.count("%s")
                    print(f"\n======== Outer SQL ({n_slots} parameters):\n{SQLformat(sql, reindent=True, keyword_case='upper')}")
                    print(f"\n======== Outer Executable SQL:\n{SQLformat(executeable_SQL(sql, params), reindent=True, keyword_case='upper')}")
                    print("========================== as_sql: DONE =====================================")
                    print(f"============================= END as_sql ======================================\n\n")
    
                # print(super_sql % super_params)
                # print(from_sql % from_params)
                # print(sql % params)
                return sql, params
            else:
                print("WARNING: From query expected and not provided")
        except Exception as E:
            print_diagnostics("as_sql ERROR")
            print(traceback.format_exc())
            return None, None

class FromQuery(Query):
    '''
    It is the Query object that supplies the SQL compiler, and so we need to override it
    to return our custom SQL compiler. A little of the logic from the get_compiler method
    that we are overriding has to be duplicated as there is no way around that.
    '''
    def get_compiler(self, using=None, connection=None, elide_empty=True, use_default_compiler=False):
        if using is None and connection is None:
            raise ValueError("Need either using or connection")
        if using:
            connections = ConnectionHandler()
            connection = connections[using]
        if use_default_compiler:
            return SQLCompiler(self, connection, using, elide_empty)
        else:
            return SQLFromCompiler(self, connection, using, elide_empty)

