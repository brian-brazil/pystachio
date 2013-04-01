from collections import Mapping
import copy
from inspect import isclass
import json

from .base import Object, Environment
from .naming import Ref, Namable, frozendict
from .typing import (
  Type,
  TypeCheck,
  TypeFactory,
  TypeMetaclass)


class Empty(object):
  """The Empty sentinel representing an unspecified field."""
  pass


class TypeSignature(object):
  """
    Type metadata for composite type schemas.
  """

  def __init__(self, cls, required=False, default=Empty):
    assert isclass(cls)
    assert issubclass(cls, Object)
    if default is not Empty and not isinstance(default, cls):
      self._default = cls(default)
    else:
      self._default = default
    self._cls = cls
    self._required = required

  def serialize(self):
    return (self.required,
            self.default.get() if not self.empty else (),
            self.empty,
            self.klazz.serialize_type())

  @staticmethod
  def deserialize(sig, type_dict):
    req, default, empty, klazz_schema = sig
    real_class = TypeFactory.new(type_dict, *klazz_schema)
    if not empty:
      return TypeSignature(real_class, default=real_class(default), required=req)
    else:
      return TypeSignature(real_class, required=req)

  def __eq__(self, other):
    return (self.klazz.serialize_type() == other.klazz.serialize_type() and
            self.required == other.required and
            self.default == other.default and
            self.empty == other.empty)

  def __ne__(self, other):
    return not (self == other)

  def __repr__(self):
    return 'TypeSignature(%s, required: %s, default: %s, empty: %s)' % (
      self.klazz.__name__, self.required, self.default, self.empty)

  @property
  def klazz(self):
    return self._cls

  @property
  def required(self):
    return self._required

  @property
  def default(self):
    return self._default

  @property
  def empty(self):
    return self._default is Empty

  @staticmethod
  def wrap(sig):
    if isclass(sig) and issubclass(sig, Object):
      return TypeSignature(sig)
    elif isinstance(sig, TypeSignature):
      return sig


def Required(cls):
  """
    Helper to make composite types read succintly.  Wrap a type and make its
    specification required during type-checking of composite types.
  """
  return TypeSignature(cls, required=True)


def Default(cls, default):
  """
    Helper to make composite types read succintly.  Wrap a type and assign it a
    default if it is unspecified in the construction of the composite type.
  """
  return TypeSignature(cls, required=False, default=default)


class StructFactory(TypeFactory):
  PROVIDES = 'Struct'

  @staticmethod
  def create(type_dict, *type_parameters):
    """
      StructFactory.create(*type_parameters) expects:

        class name,
        ((binding requirement1,),
          (binding requirement2, bound_to_scope),
           ...),
        ((attribute_name1, attribute_sig1 (serialized)),
         (attribute_name2, attribute_sig2 ...),
         ...
         (attribute_nameN, ...))
    """
    name, parameters = type_parameters
    for param in parameters:
      assert isinstance(param, tuple)
    typemap = dict((attr, TypeSignature.deserialize(param, type_dict))
                   for attr, param in parameters)
    attributes = {'TYPEMAP': typemap}
    return TypeMetaclass(str(name), (Structural,), attributes)


class StructMetaclass(type):
  """
    Schema-extracting metaclass for Struct objects.
  """
  @staticmethod
  def attributes_to_parameters(attributes):
    parameters = []
    for attr_name, attr_value in attributes.items():
      sig = TypeSignature.wrap(attr_value)
      if sig:
        parameters.append((attr_name, sig.serialize()))
    return tuple(parameters)

  def __new__(mcs, name, parents, attributes):
    if any(parent.__name__ == 'Struct' for parent in parents):
      type_parameters = StructMetaclass.attributes_to_parameters(attributes)
      return TypeFactory.new({}, 'Struct', name, type_parameters)
    else:
      return type.__new__(mcs, name, parents, attributes)


StructMetaclassWrapper = StructMetaclass('StructMetaclassWrapper', (object,), {})
class Structural(Object, Type, Namable):
  """A Structural base type for composite objects."""
  __slots__ = ('_value',)

  @classmethod
  def _process_schema_attribute(cls, attr, value):
    if attr not in cls.TYPEMAP:
      raise AttributeError('Unknown schema attribute %s' % attr)
    schema_type = cls.TYPEMAP[attr]
    if value is Empty:
      return Empty
    elif isinstance(value, schema_type.klazz):
      return value
    else:
      return schema_type.klazz(value)

  @classmethod
  def _update_schema_data(cls, value, **kw):
    for attr, val in kw.items():
      value[attr] = cls._process_schema_attribute(attr, val)

  # TODO(wickman) Perhaps apply_json and unapply_json as well so that you can do things
  # like
  #
  # class Resources(Struct):
  #   cpu = Required(Float)
  #   ram = Required(Integer)
  #
  # task = task(resources = '{{wat}}').bind(wat = Resources(cpu = 1.0, ram = 1024))
  #
  # it sort of makes sense.. the apply/unapply should work against subtrees speaking
  # in json.
  @classmethod
  def apply(cls, *args, **kw):
    value = frozendict((attr, v.default) for (attr, v) in cls.TYPEMAP.items())
    for arg in args:
      if not isinstance(arg, Mapping):
        raise ValueError('Expected dictionary argument, got %s' % repr(arg))
      cls._update_schema_data(value, **arg)
    cls._update_schema_data(value, **copy.copy(kw))
    return value

  @classmethod
  def unapply(cls, value):
    return frozendict((k, v.get()) for k, v in value.items() if v is not Empty)

  def __call__(self, **kw):
    new_self = self.copy()
    self._update_schema_data(new_self._value, **copy.copy(kw))
    return new_self

  def __eq__(self, other):
    if not isinstance(other, Structural): return False
    if self.TYPEMAP != other.TYPEMAP: return False
    si = self.interpolate()
    oi = other.interpolate()
    return si[0]._value == oi[0]._value

  def __repr__(self):
    si, _ = self.interpolate()
    return '%s(%s)' % (
      self.__class__.__name__,
      (',\n%s' % (' ' * (len(self.__class__.__name__) + 1))).join(
          '%s=%s' % (key, val) for key, val in si._value.items() if val is not Empty)
    )

  def __getattr__(self, attr):
    if not hasattr(self, 'TYPEMAP'):
      raise AttributeError

    if attr.startswith('has_'):
      if attr[4:] in self.TYPEMAP:
        return lambda: self._value[attr[4:]] != Empty

    if attr not in self.TYPEMAP:
      raise AttributeError("%s has no attribute %s" % (self.__class__.__name__, attr))

    return lambda: self.interpolate_key(attr)

  def check(self):
    for name, signature in self.TYPEMAP.items():
      if self._value[name] is Empty and signature.required:
        return TypeCheck.failure('%s[%s] is required.' % (self.__class__.__name__, name))
      elif self._value[name] is not Empty:
        type_check = self._value[name].in_scope(*self.scopes()).check()
        if type_check.ok():
          continue
        else:
          return TypeCheck.failure('%s[%s] failed: %s' % (self.__class__.__name__, name,
            type_check.message()))
    return TypeCheck.success()

  def scopes(self):
    self_scope = dict((key, value) for (key, value) in self._value.items()
                      if value is not Empty)
    return (Environment(self_scope),) + self._scopes

  def interpolate(self):
    unbound = set()
    interpolated_schema_data = {}
    scopes = self.scopes()
    for key, value in self._value.items():
      if value is Empty:
        interpolated_schema_data[key] = Empty
      else:
        vinterp, vunbound = value.in_scope(*scopes).interpolate()
        unbound.update(vunbound)
        interpolated_schema_data[key] = vinterp
    return self.__class__(**interpolated_schema_data), list(unbound)

  def interpolate_key(self, attribute):
    if self._value[attribute] is Empty:
      return Empty
    vinterp, _ = self._value[attribute].in_scope(*self.scopes()).interpolate()
    return self._process_schema_attribute(attribute, vinterp)

  @classmethod
  def type_factory(cls):
    return 'Struct'

  @classmethod
  def type_parameters(cls):
    attrs = []
    if hasattr(cls, 'TYPEMAP'):
      attrs = sorted([(attr, sig.serialize()) for attr, sig in cls.TYPEMAP.items()])
    return (cls.__name__, tuple(attrs))

  @classmethod
  def _filter_against_schema(cls, values):
    return dict((key, val) for (key, val) in values.items()
                if key in cls.TYPEMAP)

  @classmethod
  def json_load(cls, fp, strict=False):
    return cls(json.load(fp) if strict else cls._filter_against_schema(json.load(fp)))

  @classmethod
  def json_loads(cls, json_string, strict=False):
    return cls(json.loads(json_string) if strict
               else cls._filter_against_schema(json.loads(json_string)))

  def json_dump(self, fp):
    d, _ = self.interpolate()
    return json.dump(d.get(), fp)

  def json_dumps(self):
    d, _ = self.interpolate()
    return json.dumps(d.get())

  def find(self, ref):
    if not ref.is_dereference():
      raise Namable.NamingError(self, ref)
    name = ref.action().value
    if name not in self.TYPEMAP or self._value[name] is Empty:
      raise Namable.NotFound(self, ref)
    else:
      namable = self._value[name]
      if ref.rest().is_empty():
        return namable.in_scope(*self.scopes())
      else:
        if not isinstance(namable, Namable):
          raise Namable.Unnamable(namable)
        else:
          return namable.in_scope(*self.scopes()).find(ref.rest())


class Struct(StructMetaclassWrapper, Structural):
  """
    Schema-based composite objects, e.g.

      class Employee(Struct):
        first = Required(String)
        last  = Required(String)
        email = Required(String)
        phone = String

      Employee(first = "brian", last = "wickman", email = "wickman@twitter.com").check()

    They're purely functional data structures and behave more like functors.
    In other words they're immutable:

      >>> brian = Employee(first = "brian")
      >>> brian(last = "wickman")
      Employee(last=String(wickman), first=String(brian))
      >>> brian
      Employee(first=String(brian))
  """
  pass
