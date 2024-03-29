"""Node model and Node admin interaction."""

from django import forms
from django.conf.urls import url
from django.contrib.admin.utils import quote, unquote
from django.core.exceptions import PermissionDenied
from django.core.validators import MinLengthValidator, RegexValidator
from django.db import models
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string

from treebeard.mp_tree import MP_Node

from wagtail.admin.edit_handlers import FieldPanel
from wagtail.admin.forms import WagtailAdminModelForm
from wagtail.contrib.modeladmin.helpers import ButtonHelper
from wagtail.contrib.modeladmin.options import ModelAdmin
from wagtail.contrib.modeladmin.views import CreateView
from wagtail.search import index


node_name_validator = RegexValidator(
    regex='^[\w][a-zA-Z &]+$',
    message="Letters, numbers and '&' only plus must start with a letter.",
)


class Node(index.Indexed, MP_Node):
    """Represents a single nestable Node in the corporate taxonomy."""

    # node editable fields
    name = models.CharField(
        max_length=50,
        unique=True,
        help_text='Keep the name short, ideally one word.',
        validators=[node_name_validator, MinLengthValidator(5)]
    )
    aliases = models.TextField(
        'Also known as',
        max_length=255,
        blank=True,
        help_text="What else is this known as or referred to as?"
    )

    # node tree specific fields and attributes
    node_order_index = models.IntegerField(
        blank=True,
        default=0,
        editable=False
    )
    node_child_verbose_name = 'child'
    node_order_by = ['node_order_index', 'name']

    panels = [
        # FieldPanel('parent'),  # virtual field - see TopicForm
        FieldPanel('name'),
        FieldPanel('aliases', widget=forms.Textarea(attrs={'rows': '5'})),
    ]

    def get_as_listing_header(self):
        """Build HTML representation of node with title & depth indication."""
        depth = self.get_depth()
        rendered = render_to_string(
            'includes/node_list_header.html',
            {
                'depth': depth,
                'depth_minus_1': depth - 1,
                'is_root': self.is_root(),
                'name': self.name,
            }
        )
        return rendered
    get_as_listing_header.short_description = 'Name'
    get_as_listing_header.admin_order_field = 'name'

    def get_parent(self, *args, **kwargs):
        """Duplicate of get_parent from treebeard API."""
        return super().get_parent(*args, **kwargs)
    get_parent.short_description = 'Parent'

    search_fields = [
        index.SearchField('name', partial_match=True),
        index.SearchField('aliases', partial_match=False, boost=0.25),
    ]

    def delete(self):
        """Prevent users from deleting the root node."""
        if self.is_root():
            raise PermissionDenied('Cannot delete root Topic.')
        else:
            super().delete()

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'Topic'
        verbose_name_plural = 'Topics'


class BasicNodeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        depth_line = '-' * (obj.get_depth() - 1)
        return "{} {}".format(depth_line, super().label_from_instance(obj))


class NodeForm(WagtailAdminModelForm):

    parent = BasicNodeChoiceField(
        required=True,
        queryset=Node.objects.all(),
        empty_label=None,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs['instance']

        if instance.is_root() or Node.objects.count() is 0:
            # hide and disable the parent field
            self.fields['parent'].disabled = True
            self.fields['parent'].required = False
            self.fields['parent'].empty_label = 'N/A - Root Node'
            self.fields['parent'].widget = forms.HiddenInput()

            # update label to indicate this is the root
            self.fields['name'].label += ' (Root)'
        elif instance.id:
            self.fields['parent'].initial = instance.get_parent()

    def save(self, commit=True, *args, **kwargs):
        instance = super().save(commit=False, *args, **kwargs)
        parent = self.cleaned_data['parent']

        if not commit:
            # simply return the instance if not actually saving (committing)
            return instance

        if instance.id is None:  # creating a new node
            if Node.objects.all().count() is 0:  # no nodes, creating root
                Node.add_root(instance=instance)  # add a NEW root node
            else:  # nodes exist, must be adding node under a parent
                instance = parent.add_child(instance=instance)
        else:  # editing an existing node
            instance.save()  # update existing node
            if instance.get_parent() != parent:
                instance.move(parent, pos='sorted-child')
        return instance


Node.base_form_class = NodeForm


class NodeButtonHelper(ButtonHelper):
    """Custom button functionality for node listing buttons."""

    def prepare_classnames(self, start=None, add=None, exclude=None):
        """Parse classname sets into final css classess list."""
        classnames = start or []
        classnames.extend(add or [])
        return self.finalise_classname(classnames, exclude or [])

    def delete_button(self, pk, *args, **kwargs):
        """Ensure that the delete button is not shown for root node."""
        instance = self.model.objects.get(pk=pk)
        if instance.is_root():
            return
        return super().delete_button(pk, *args, **kwargs)

    def inspect_button(self, *args, **kwargs):
        """Replace the term 'Inspect' with 'Details' in listing buttons."""
        button = super().inspect_button(*args, **kwargs)
        button['label'] = button['label'].replace('Inspect', 'Details')
        button['title'] = button['label'].replace('Inspect', 'Details', 1)
        return button

    def add_child_button(self, pk, child_verbose_name, **kwargs):
        """Build a add child button, to easily add a child under node."""
        classnames = self.prepare_classnames(
            start=self.edit_button_classnames + ['icon', 'icon-plus'],
            add=kwargs.get('classnames_add'),
            exclude=kwargs.get('classnames_exclude')
        )
        return {
            'classname': classnames,
            'label': 'Add %s %s' % (
                child_verbose_name, self.verbose_name),
            'title': 'Add %s %s under this one' % (
                child_verbose_name, self.verbose_name),
            'url': self.url_helper.get_action_url('add_child', quote(pk)),
        }

    def get_buttons_for_obj(self, obj, exclude=None, *args, **kwargs):
        """Override the getting of buttons, prepending create child button."""
        buttons = super().get_buttons_for_obj(obj, *args, **kwargs)

        add_child_button = self.add_child_button(
            pk=getattr(obj, self.opts.pk.attname),
            child_verbose_name=getattr(obj, 'node_child_verbose_name'),
            **kwargs
        )
        buttons.append(add_child_button)

        return buttons


class AddChildNodeViewClass(CreateView):
    """View class that can take an additional URL param for parent id."""

    parent_pk = None
    parent_instance = None

    def __init__(self, model_admin, parent_pk):
        self.parent_pk = unquote(parent_pk)
        object_qs = model_admin.model._default_manager.get_queryset()
        object_qs = object_qs.filter(pk=self.parent_pk)
        self.parent_instance = get_object_or_404(object_qs)
        super().__init__(model_admin)

    def get_page_title(self):
        """Generate a title that explains you are adding a child."""
        title = super().get_page_title()
        return title + ' %s %s for %s' % (
            self.model.node_child_verbose_name,
            self.opts.verbose_name,
            self.parent_instance
        )

    def get_initial(self):
        """Set the selected parent field to the parent_pk."""
        return {'parent': self.parent_pk}


class NodeAdmin(ModelAdmin):
    """Class for presenting topics in admin using modeladmin."""

    model = Node

    # admin menu options
    menu_icon = 'fa-cube'  # using wagtail fontawesome
    menu_order = 800

    # listing view options
    list_display = ('get_as_listing_header', 'get_parent', 'aliases')
    list_per_page = 50
    search_fields = ('name', 'aliases')

    # inspect view options
    inspect_view_enabled = True
    inspect_view_fields = ('name', 'get_parent', 'aliases', 'id')

    # other overrides
    button_helper_class = NodeButtonHelper

    def add_child_view(self, request, instance_pk):
        """Generate a class-based view to provide 'add child' functionality."""
        # instance_pk will become the default selected parent_pk
        kwargs = {'model_admin': self, 'parent_pk': instance_pk}
        view_class = AddChildNodeViewClass
        return view_class.as_view(**kwargs)(request)

    def get_admin_urls_for_registration(self):
        """Add the new url for add child page to the registered URLs."""
        urls = super().get_admin_urls_for_registration()
        add_child_url = url(
            self.url_helper.get_action_url_pattern('add_child'),
            self.add_child_view,
            name=self.url_helper.get_action_url_name('add_child')
        )
        return urls + (add_child_url, )
 node_list_header.html
{% if is_root %}
  <span style="font-size:135%;"><strong>{{ name }}</strong></span>
{% else %}
  <span>
    <span class="inline-block" style="margin-left:{{ depth_minus_1 }}em; font-size:{% if depth is 1 %}120{% elif depth is 2 %}110{% else %}100{% endif %}%;"></span>
    <i class="icon icon-fa-level-up icon-fa-rotate-90" style="display: inline-block;"></i>
    {{ name }}
  </span>
{% endif %}
 wagtail_hooks.py
from .models import NodeAdmin

from wagtail.contrib.modeladmin.options import modeladmin_register


modeladmin_register(NodeAdmin)
