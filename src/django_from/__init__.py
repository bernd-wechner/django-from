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

from sqlparse import parse, tokens, format as SQLformat
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
        # Start the queryset anew
        new_queryset = queryset.model._default_manager.all()
        # Let Django know we want to use our custom Query (which we need to do to attach our custom SQL compiler)
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
        # Attach a debug flag so the complier can emit debugging info if desired
        new_queryset.query.debug = debug
        # return the new queryset down the chain
        return new_queryset

class FromCompiler(SQLCompiler):
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
        debug = self.query.debug
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
        from_query = self.query._from_query
        from_sql, from_params = from_query.sql_with_params()

        # The SQL should contain one %s field per param in super_params. Assert this
        # as it is the understanding of what super() (the standard SQL compiler for 
        # querysets) provides
        assert isinstance(from_sql, str)
        n_from_slots = from_sql.count("%s")
        n_from_vals = len(from_params)
        assert n_from_slots==n_from_vals

        if debug:
            print("django_from.as_sql:")
            print(f"Inner SQL ({n_from_slots} parameters):\n{SQLformat(from_sql, reindent=True, keyword_case='upper')}")
            
            print(f"Default Outer SQL ({n_super_slots} parameters):\n{SQLformat(sql, reindent=True, keyword_case='upper')}")

        
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
        if debug: print(f"\tGROUP BY correction ...")
        
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
            print(f"Considering Annotations:")
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
            
            if outer_aggregates: 
                print(f"\t{len(outer_aggregates)} Outer aggregates")
                for annotation, value in outer_aggregates.items():
                    print(f"\t\t{annotation} = {value}")
                    
        # If there are any other kinds of annotations they are not yet supported and we 
        # want to consider them once encountered. 
        assert len(outer_values) + len(outer_aggregates) == len(self.query.annotations) 

        # Now in the Outer SQL we want to remove the parmas that are placeholder 
        # values and make them literals
        if debug: print(f"Fixing {len(outer_values)} parameter simple value substitutions in the Outer query:")
        for annotation, value in outer_values.items():
            # Tricky we need to this in order. params is a list
            # in the order of the incidence of %s in the query. 
            # So the mapping is in order. We need to find the 
            # index of anything we're removing (how many %s before 
            # it and how many after it for rigour and replace it in the SQL 
            # and remove the item from the list)
            #
            # We may need to remove it from self.query.annotations too.  
            if annotation == value.value.removesuffix('_placeholder'):
                placeholder = f'%s AS "{annotation}"'
                matches = re.findall(placeholder, sql)
                assert len(matches) == 1
                assert matches[0] == placeholder
                before = sql[:sql.index(placeholder)].count('%s')
                after = sql[sql.index(placeholder)+len(placeholder):].count('%s')
                assert before + after + 1 == len(params), f"Counting error {before=}, {after=}, {len(params)=}"

                # Remove the placeholder cleanly from the query and its SQL
                sql = sql.replace(placeholder, f'"{annotation}"')
                assert params[before] == value.value
                del params[before]
                del self.query.annotations[annotation]
                del self.annotation_col_map[annotation]
                if debug: print(f"\tReplaced placeholder for {annotation}, removed it from params ({len(params)} left) and annotations ({len(self.query.annotations)} left, {len(self.annotation_col_map)} in the col_map)")
                

        # Because we only support simple value and aggregates having substituted all the simple value params
        # Only aggregate params should be left.
        assert len(params) == len(outer_aggregates) 
        if debug: print(f"Fixing {len(outer_aggregates)} parameter aggregate substitutions in the Outer query:")
        for param in params:
            if param.endswith("_placeholder"):
                val = param.removesuffix("_placeholder")
                # params are in order of the %s fields, and so we replace only one instace, 
                # the first instance and rmeain in lock step.  
                sql = sql.replace("%s", f'"{val}"', 1)
                del params[0]
                # This should not appear as annotation because it's inside an aggragte annotation
                assert not val in self.query.annotations
                if debug: print(f"\tfixed {val}") 
            else:
                # In the From() method we replaced all params with placeholders. If there are 
                # any that arent' then left and right hand have not met ;-) 
                raise ValueError("Premise that all params in query are placeholders is broken.")

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

        # print(super_sql % super_params)
        # print(from_sql % from_params)
        # print(sql % params)
        return sql, params

class FromQuery(Query):
    '''
    It is the Query object that supplies the SQL compiler, and so we need to override it
    to return our custom SQL compiler. A little of the logic from the get_compiler method
    that we are overriding has to be duplicated as there is no way around that.
    '''
    def get_compiler(self, using=None, connection=None, elide_empty=True):
        if using is None and connection is None:
            raise ValueError("Need either using or connection")
        if using:
            connections = ConnectionHandler()
            connection = connections[using]
        return FromCompiler(self, connection, using, elide_empty)

