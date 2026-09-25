# The public PHP site, packaged so hub can serve the same code Hostinger does.
#
# Hostinger runs this on plain shared Apache + mod_php, which is why the routing
# lives in `.htaccess` rather than in a vhost: that file is the one piece of
# server config the shared host lets us set. Baking the rewrite into the image's
# vhost instead would give this container its own copy of the routing rules, and
# the two would drift the first time a path changed. So AllowOverride is on and
# the same `.htaccess` drives both hosts.
FROM php:8.3-apache

RUN a2enmod rewrite

# `.htaccess` is inert unless overrides are allowed — without this the front
# controller never runs and every path except `/` is a 404 from Apache, which
# looks exactly like a routing bug in index.php.
RUN printf '<Directory /var/www/html>\n\tAllowOverride All\n</Directory>\n' \
    > /etc/apache2/conf-available/allow-htaccess.conf \
 && a2enconf allow-htaccess

# Sessions default to /tmp, which is inside the container and therefore emptied
# by every `docker compose up --force-recreate`. Deploys are manual here, but
# they should not log the family out of the site as a side effect, so park the
# session files somewhere a named volume can outlive the container.
RUN mkdir -p /var/lib/php/sessions \
 && chown www-data:www-data /var/lib/php/sessions \
 && printf 'session.save_path = "/var/lib/php/sessions"\n' \
    > /usr/local/etc/php/conf.d/zz-sessions.ini

COPY --chown=www-data:www-data . /var/www/html/

# manage_users.py is an admin tool run from a checkout, not part of the site.
# Serving it would hand out the shape of the user store for free.
RUN rm -f /var/www/html/manage_users.py /var/www/html/Dockerfile \
          /var/www/html/docker-compose.yml /var/www/html/.dockerignore

# The translation catalogues. Copied in rather than mounted: each app in this
# stack serves its own copy, because they are different containers on different
# machines and one fetching strings from another would make this page depend on
# the hub being up to be legible.
COPY i18n/ /var/www/i18n/
ENV HOME_STACK_I18N_DIR=/var/www/i18n
