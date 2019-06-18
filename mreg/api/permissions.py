from django.conf import settings

from rest_framework import exceptions
from rest_framework.permissions import IsAuthenticated, SAFE_METHODS

from mreg.api.v1.serializers import HostSerializer
from mreg.models import HostGroup, NetGroupRegexPermission


def get_settings_groups(group_setting_name):
    groupnames = getattr(settings, group_setting_name, None)
    if groupnames is None:
        raise exceptions.APIException(detail=f'{group_setting_name} is unset in config')
    if isinstance(groupnames, str):
        groupnames = (groupnames, )
    return groupnames


def user_in_settings_group(request, group_setting_name):
    groupnames = get_settings_groups(group_setting_name)
    return request.user.groups.filter(name__in=groupnames).exists()


def _list_in_list(list_a, list_b):
    # Returns true if any of element in list_a is in list_b
    return any(i in list_b for i in list_a)


def user_in_required_group(user):
    return _list_in_list(get_settings_groups('REQUIRED_USER_GROUPS'),
                         user.group_list)


def user_is_superuser(user):
    groups = get_settings_groups('SUPERUSER_GROUP')
    return _list_in_list(groups, user.group_list)


def user_is_adminuser(user):
    groups = get_settings_groups('ADMINUSER_GROUP')
    return _list_in_list(groups, user.group_list)


def user_is_group_adminuser(user):
    groups = get_settings_groups('GROUPADMINUSER_GROUP')
    return _list_in_list(groups, user.group_list)


def is_super_or_admin(user):
    return user_is_superuser(user) or user_is_adminuser(user)


def is_super_or_group_admin(user):
    return user_is_superuser(user) or user_is_group_adminuser(user)


class IsAuthenticatedAndReadOnly(IsAuthenticated):
    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return request.method in SAFE_METHODS


class IsInRequiredGroup(IsAuthenticated):
    """
    Allows only access to users in the required group.
    """

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return user_in_settings_group(request, 'REQUIRED_USER_GROUPS')


class ReadOnlyForRequiredGroup(IsInRequiredGroup):
    """
    Allows read only access to users in the required group.
    """

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return request.method in SAFE_METHODS


class IsSuperGroupMember(IsAuthenticated):
    """
    Permit user if in super user group.
    """

    group = 'SUPERUSER_GROUP'

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return user_in_settings_group(request, 'SUPERUSER_GROUP')


class IsSuperOrGroupAdminOrReadOnly(IsAuthenticated):
    """
    Permit user if in super or group admin group, else read only.
    """

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in SAFE_METHODS:
            return True
        return is_super_or_group_admin(request.user)


class IsGrantedNetGroupRegexPermission(IsAuthenticated):
    """
    Permit user if the user has been granted access through a
    NetGroupRegexPermission.
    """

    def has_permission(self, request, view):
        # This method is called before the view is executed, so
        # just do some preliminary checks.
        if not super().has_permission(request, view):
            return False
        if request.method in SAFE_METHODS:
            return True
        if is_super_or_admin(request.user):
            return True
        # Will do do more object checks later, but initially refuse any
        # unwarranted requests.
        if NetGroupRegexPermission.objects.filter(group__in=request.user.group_list
                                                  ).exists():
            return True
        return False

    @staticmethod
    def has_perm(user, hostname, ips):
        return bool(NetGroupRegexPermission.find_perm(user.group_list,
                                                      hostname, ips))

    def has_obj_perm(self, user, obj):
        return self.has_perm(user, *self._get_hostname_and_ips(obj))

    def has_create_permission(self, request, view, validated_serializer):
        import mreg.api.v1.views
        if user_is_superuser(request.user):
            return True
        hostname = None
        ips = []
        data = validated_serializer.validated_data
        if '*' in data.get('name', ''):
            return False
        if user_is_adminuser(request.user):
            return True
        if isinstance(view, (mreg.api.v1.views.HostList,
                             mreg.api.v1.views.IpaddressList)):
            # HostList does not require ipaddress, but if none, the permissions
            # will not match, so just refuse it.
            if 'ipaddress' not in data:
                return False
            ips.append(data['ipaddress'])
            hostname = data['host'].name
        elif 'host' in data:
            hostname, ips = self._get_hostname_and_ips(data['host'])
        else:
            raise exceptions.PermissionDenied(f"Unhandled view: {view}")

        if ips and hostname:
            return self.has_perm(request.user, hostname, ips)
        return False

    def has_destroy_permission(self, request, view, validated_serializer):
        import mreg.api.v1.views
        if is_super_or_admin(request.user):
            return True
        obj = view.get_object()
        if isinstance(view, mreg.api.v1.views.HostDetail):
            pass
        elif hasattr(obj, 'host'):
            obj = obj.host
        else:
            raise exceptions.PermissionDenied(f"Unhandled view: {view}")

        return self.has_obj_perm(request.user, obj)

    def has_update_permission(self, request, view, validated_serializer):
        import mreg.api.v1.views
        if user_is_superuser(request.user):
            return True
        data = validated_serializer.validated_data
        if '*' in data.get('name', ''):
            return False
        if user_is_adminuser(request.user):
            return True
        obj = view.get_object()
        if isinstance(view, mreg.api.v1.views.HostDetail):
            hostname, ips = self._get_hostname_and_ips(obj)
            # If renaming a host, make sure the user has permission to both the
            # new and and old hostname.
            if 'name' in data:
                if not self.has_perm(request.user, data['name'], ips):
                    return False
            return self.has_perm(request.user, hostname, ips)
        elif hasattr(obj, 'host'):
            return self.has_obj_perm(request.user, obj.host)
        raise exceptions.PermissionDenied(f"Unhandled view: {view}")

    def _get_hostname_and_ips(self, hostobject):
        ips = []
        host = HostSerializer(hostobject)
        for i in host.data['ipaddresses']:
            ips.append(i['ipaddress'])
        return host.data['name'], ips


class HostGroupPermission(IsAuthenticated):

    def has_permission(self, request, view):
        # This method is called before the view is executed, so
        # just do some preliminary checks.
        if not super().has_permission(request, view):
            return False
        if request.method in SAFE_METHODS:
            return True
        if is_super_or_group_admin(request.user):
            return True
        # Will do do more object checks later, but initially refuse any
        # unwarranted requests.
        if HostGroup.objects.filter(owners__name__in=request.user.group_list).exists():
            return True
        return False

    @staticmethod
    def is_super_or_group_admin(request):
        return is_super_or_group_admin(request.user)

    @staticmethod
    def _request_user_is_owner(hostgroup, request):
        owners = set(hostgroup.owners.values_list('name', flat=True))
        return _list_in_list(request.user.group_list, owners)

    def has_m2m_change_permission(self, request, view):
        if is_super_or_group_admin(request.user):
            return True
        return self._request_user_is_owner(view.hostgroup, request)

    # patch will only happen on HostGroupDetail
    def has_update_permission(self, request, view, validated_serializer):
        if is_super_or_group_admin(request.user):
            return True
        if 'description' in validated_serializer.validated_data:
            return self._request_user_is_owner(view.get_object(), request)
        return False

    def has_destroy_permission(self, request, view, validated_serializer):
        if is_super_or_group_admin(request.user):
            return True
        return False
