# Copyright (C) 2015, Carlo de Franchis <carlo.de-franchis@cmla.ens-cachan.fr>
# Copyright (C) 2015, Gabriele Facciolo <facciolo@cmla.ens-cachan.fr>
# Copyright (C) 2015, Enric Meinhardt <enric.meinhardt@cmla.ens-cachan.fr>


from __future__ import print_function
import os
import sys
import numpy as np

from s2plib import rpc_model
from s2plib import rpc_utils
from s2plib import estimation
from s2plib import evaluation
from s2plib import common
from s2plib import sift
from s2plib import visualisation
from s2plib import block_matching
from s2plib.config import cfg


def center_2d_points(pts):
    """
    Translates 2D points.

    The input points are translated such that the output points are centered at
    origin.

    Args:
        pts: 2D array of dimension Nx2 containing the coordinates of the input
            points, one point per line

    Returns:
        new_x, new_y, T: coordinates of the transformed points, together with
            the similarity (translation) matrix. This transformation takes the
            input points on the output points.
    """
    # centroid
    cx = np.mean(pts[:, 0])
    cy = np.mean(pts[:, 1])

    # shift origin to centroid
    new_x = pts[:, 0] - cx
    new_y = pts[:, 1] - cy

    # translation matrix
    T = np.eye(3)     #              1     0    -cx
    T[0, 2] = -cx     # matrix T  =  0     1    -cy
    T[1, 2] = -cy     #              0     0     1

    return np.vstack([new_x, new_y]).T, T


def filter_matches_epipolar_constraint(F, matches, thresh):
    """
    Discards matches that are not consistent with the epipolar constraint.

    Args:
        F: fundamental matrix
        matches: list of pairs of 2D points, stored as a Nx4 numpy array
        thresh: maximum accepted distance between a point and its matched
            epipolar line

    Returns:
        the list of matches that satisfy the constraint. It is a sub-list of
        the input list.
    """
    out = []
    for match in matches:
        x = np.array([match[0], match[1], 1])
        xx = np.array([match[2], match[3], 1])
        d1 = evaluation.distance_point_to_line(x, np.dot(F.T, xx))
        d2 = evaluation.distance_point_to_line(xx, np.dot(F, x))
        if max(d1, d2) < thresh:
            out.append(match)

    return np.array(out)


def register_horizontally_shear(matches, H1, H2):
    """
    Adjust rectifying homographies with a shear to modify the disparity range.

    Args:
        matches: list of pairs of 2D points, stored as a Nx4 numpy array
        H1, H2: two homographies, stored as numpy 3x3 matrices

    Returns:
        H2: corrected homography H2

    The matches are provided in the original images coordinate system. By
    transforming these coordinates with the provided homographies, we obtain
    matches whose disparity is only along the x-axis.
    """
    # transform the matches according to the homographies
    p1 = common.points_apply_homography(H1, matches[:, :2])
    x1 = p1[:, 0]
    y1 = p1[:, 1]
    p2 = common.points_apply_homography(H2, matches[:, 2:])
    x2 = p2[:, 0]
    y2 = p2[:, 1]

    if cfg['debug']:
        print("Residual vertical disparities: max, min, mean. Should be zero")
        print(np.max(y2 - y1), np.min(y2 - y1), np.mean(y2 - y1))

    # we search the (s, b) vector that minimises \sum (x1 - (x2+s*y2+b))^2
    # it is a least squares minimisation problem
    A = np.vstack((y2, y2*0+1)).T
    B = x1 - x2
    s, b = np.linalg.lstsq(A, B)[0].flatten()

    # correct H2 with the estimated shear
    return np.dot(np.array([[1, s, b], [0, 1, 0], [0, 0, 1]]), H2)


def register_horizontally_translation(matches, H1, H2, flag='center'):
    """
    Adjust rectifying homographies with a translation to modify the disparity range.

    Args:
        matches: list of pairs of 2D points, stored as a Nx4 numpy array
        H1, H2: two homographies, stored as numpy 3x3 matrices
        flag: option needed to control how to modify the disparity range:
            'center': move the barycenter of disparities of matches to zero
            'positive': make all the disparities positive
            'negative': make all the disparities negative. Required for
                Hirshmuller stereo (java)

    Returns:
        H2: corrected homography H2

    The matches are provided in the original images coordinate system. By
    transforming these coordinates with the provided homographies, we obtain
    matches whose disparity is only along the x-axis. The second homography H2
    is corrected with a horizontal translation to obtain the desired property
    on the disparity range.
    """
    # transform the matches according to the homographies
    p1 = common.points_apply_homography(H1, matches[:, :2])
    x1 = p1[:, 0]
    y1 = p1[:, 1]
    p2 = common.points_apply_homography(H2, matches[:, 2:])
    x2 = p2[:, 0]
    y2 = p2[:, 1]

    # for debug, print the vertical disparities. Should be zero.
    if cfg['debug']:
        print("Residual vertical disparities: max, min, mean. Should be zero")
        print(np.max(y2 - y1), np.min(y2 - y1), np.mean(y2 - y1))

    # compute the disparity offset according to selected option
    t = 0
    if (flag == 'center'):
        t = np.mean(x2 - x1)
    if (flag == 'positive'):
        t = np.min(x2 - x1)
    if (flag == 'negative'):
        t = np.max(x2 - x1)

    # correct H2 with a translation
    return np.dot(common.matrix_translation(-t, 0), H2)


def disparity_range_from_matches(matches, H1, H2, w, h):
    """
    Compute the disparity range of a ROI from a list of point matches.

    The estimation is based on the extrapolation of the affine registration
    estimated from the matches. The extrapolation is done on the whole region of
    interest.

    Args:
        matches: Nx4 numpy array containing a list of matches, in the full
            image coordinates frame, before rectification
        w, h: width and height of the rectangular ROI in the first image.
        H1, H2: two rectifying homographies, stored as numpy 3x3 matrices

    Returns:
        disp_min, disp_max: horizontal disparity range
    """
    # transform the matches according to the homographies
    p1 = common.points_apply_homography(H1, matches[:, :2])
    x1 = p1[:, 0]
    p2 = common.points_apply_homography(H2, matches[:, 2:])
    x2 = p2[:, 0]
    y2 = p2[:, 1]


    # compute the final disparity range
    disp_min = np.floor(np.min(x2 - x1))
    disp_max = np.ceil(np.max(x2 - x1))

    # add a security margin to the disparity range
    disp_min *= (1 - np.sign(disp_min) * cfg['disp_range_extra_margin'])
    disp_max *= (1 + np.sign(disp_max) * cfg['disp_range_extra_margin'])
    return disp_min, disp_max


def disparity_range(rpc1, rpc2, x, y, w, h, H1, H2, matches, A=None):
    """
    Compute the disparity range of a ROI from a list of point matches.

    The estimation is based on the extrapolation of the affine registration
    estimated from the matches. The extrapolation is done on the whole region of
    interest.

    Args:
        rpc1, rpc2: two instances of the rpc_model.RPCModel class
        x, y, w, h: four integers defining the rectangular ROI in the first
            image.  (x, y) is the top-left corner, and (w, h) are the dimensions
            of the rectangle.
        H1, H2: two rectifying homographies, stored as numpy 3x3 matrices
        matches: Nx4 numpy array containing a list of sift matches, in the full
            image coordinates frame
        A (optional): 3x3 numpy array containing the pointing error correction
            for im2. This matrix is usually estimated with the pointing_accuracy
            module.

    Returns:
        disp: 2-uple containing the horizontal disparity range
    """
    # srtm disparity range
    if (cfg['disp_range_method'] in ['srtm', 'wider_sift_srtm']) or matches is None or len(matches) < 2:
        srtm_disp = rpc_utils.srtm_disp_range_estimation(rpc1, rpc2, x, y, w, h,
                                                         H1, H2, A,
                                                         cfg['disp_range_srtm_high_margin'],
                                                         cfg['disp_range_srtm_low_margin'])
        print("SRTM disparity range: [%f, %f]" % (srtm_disp[0], srtm_disp[1]))

    if cfg['disp_range_method'] == 'srtm' or matches is None or len(matches) < 2:
        return srtm_disp

    # sift disparity range
    if matches is not None:
        sift_disp = disparity_range_from_matches(matches, H1, H2, w, h)
        print("SIFT disparity range: [%f, %f]" % (sift_disp[0], sift_disp[1]))
        if cfg['disp_range_method'] == 'sift':
            return sift_disp

    # expand disparity range with srtm according to cfg params
    if cfg['disp_range_method'] == 'wider_sift_srtm' and (matches is not None):
        disp = min(srtm_disp[0], sift_disp[0]), max(srtm_disp[1], sift_disp[1])
        print("Final disparity range: [%f, %f]" % (disp[0], disp[1]))
        return disp


def rectification_homographies(matches, x, y, w, h, hmargin=0, vmargin=0):
    """
    Computes rectifying homographies from point matches for a given ROI.

    The affine fundamental matrix F is estimated with the gold-standard
    algorithm, then two rectifying similarities (rotation, zoom, translation)
    are computed directly from F.

    Args:
        matches: numpy array of shape (n, 4) containing a list of 2D point
            correspondences between the two images.
        x, y, w, h: four integers defining the rectangular ROI in the first
            image. (x, y) is the top-left corner, and (w, h) are the dimensions
            of the rectangle.
        {h,v}margin: translations added to the rectifying similarities to extend the
            horizontal and vertical footprint of the rectified images

    Returns:
        S1, S2, F: three numpy arrays of shape (3, 3) representing the
        two rectifying similarities to be applied to the two images and the
        corresponding affine fundamental matrix.
    """
    # estimate the affine fundamental matrix with the Gold standard algorithm
    F = estimation.affine_fundamental_matrix(matches)

    # compute rectifying similarities
    S1, S2 = estimation.rectifying_similarities_from_affine_fundamental_matrix(F, cfg['debug'])

    if cfg['debug']:
        y1 = common.points_apply_homography(S1, matches[:, :2])[:, 1]
        y2 = common.points_apply_homography(S2, matches[:, 2:])[:, 1]
        err = np.abs(y1 - y2)
        print("max, min, mean rectification error on point matches: ", end=' ')
        print(np.max(err), np.min(err), np.mean(err))

    # pull back top-left corner of the ROI to the origin (plus margin)
    pts = common.points_apply_homography(S1, [[x, y], [x+w, y], [x+w, y+h], [x, y+h]])
    x0, y0 = common.bounding_box2D(pts)[:2]
    T = common.matrix_translation(-x0 + hmargin, -y0 + vmargin)
    return np.dot(T, S1), np.dot(T, S2), F


def rectify_pair(im1, im2, rpc1, rpc2, x, y, w, h, out1, out2, A=None,
                 sift_matches=None, method='rpc', hmargin=0, vmargin=0):
    """
    Rectify a ROI in a pair of images.

    Args:
        im1, im2: paths to two image files
        rpc1, rpc2: paths to the two xml files containing RPC data
        x, y, w, h: four integers defining the rectangular ROI in the first
            image.  (x, y) is the top-left corner, and (w, h) are the dimensions
            of the rectangle.
        out1, out2: paths to the output rectified crops
        A (optional): 3x3 numpy array containing the pointing error correction
            for im2. This matrix is usually estimated with the pointing_accuracy
            module.
        sift_matches (optional): Nx4 numpy array containing a list of sift
            matches, in the full image coordinates frame
        method (default: 'rpc'): option to decide wether to use rpc of sift
            matches for the fundamental matrix estimation.
        {h,v}margin (optional): horizontal and vertical margins added on the
            sides of the rectified images

        This function uses the parameter subsampling_factor from the
        config module. If the factor z > 1 then the output images will
        be subsampled by a factor z. The output matrices H1, H2, and the
        ranges are also updated accordingly:
        Hi = Z * Hi with Z = diag(1/z, 1/z, 1) and
        disp_min = disp_min / z  (resp _max)

    Returns:
        H1, H2: Two 3x3 matrices representing the rectifying homographies that
        have been applied to the two original (large) images.
        disp_min, disp_max: horizontal disparity range
    """
    # read RPC data
    rpc1 = rpc_model.RPCModel(rpc1)
    rpc2 = rpc_model.RPCModel(rpc2)

    # compute real or virtual matches
    if method == 'rpc':
        # find virtual matches from RPC camera models
        matches = rpc_utils.matches_from_rpc(rpc1, rpc2, x, y, w, h,
                                             cfg['n_gcp_per_axis'])

        # correct second image coordinates with the pointing correction matrix
        if A is not None:
            matches[:, 2:] = common.points_apply_homography(np.linalg.inv(A),
                                                            matches[:, 2:])
    else:
        matches = sift_matches

    # compute rectifying homographies
    H1, H2, F = rectification_homographies(matches, x, y, w, h, hmargin, vmargin)

    if cfg['register_with_shear']:
        # compose H2 with a horizontal shear to reduce the disparity range
        a = np.mean(rpc_utils.altitude_range(rpc1, x, y, w, h))
        lon, lat, alt = rpc_utils.ground_control_points(rpc1, x, y, w, h, a, a, 4)
        x1, y1 = rpc1.inverse_estimate(lon, lat, alt)[:2]
        x2, y2 = rpc2.inverse_estimate(lon, lat, alt)[:2]
        m = np.vstack([x1, y1, x2, y2]).T
        m = np.vstack({tuple(row) for row in m})  # remove duplicates due to no alt range
        H2 = register_horizontally_shear(m, H1, H2)

    # compose H2 with a horizontal translation to center disp range around 0
    if sift_matches is not None:
        sift_matches = filter_matches_epipolar_constraint(F, sift_matches,
                                                          cfg['epipolar_thresh'])
        if len(sift_matches) < 10:
            print('WARNING: no registration with less than 10 matches')
        else:
            H2 = register_horizontally_translation(sift_matches, H1, H2)

    # compute disparity range
    if cfg['debug']:
        out_dir = os.path.dirname(out1)
        np.savetxt(os.path.join(out_dir, 'sift_matches_disp.txt'),
                   sift_matches, fmt='%9.3f')
        visualisation.plot_matches(im1, im2, rpc1, rpc2, sift_matches, x, y, w, h,
                                   os.path.join(out_dir, 'sift_matches_disp.png'))
    disp_m, disp_M = disparity_range(rpc1, rpc2, x, y, w, h, H1, H2,
                                     sift_matches, A)

    # impose a minimal disparity range (TODO this is valid only with the
    # 'center' flag for register_horizontally_translation)
    disp_m = min(-3, disp_m)
    disp_M = max(3, disp_M)

    # compute rectifying homographies for non-epipolar mode (rectify the secondary tile only)
    if block_matching.rectify_secondary_tile_only(cfg['matching_algorithm']):
        H1_inv = np.linalg.inv(H1)
        H1 = np.eye(3) # H1 is replaced by 2-D array with ones on the diagonal and zeros elsewhere
        H2 = np.dot(H1_inv,H2)
        T = common.matrix_translation(-x + hmargin, -y + vmargin)
        H1 = np.dot(T, H1)
        H2 = np.dot(T, H2)

    #  if subsampling_factor'] the homographies are altered to reflect the zoom
    z = cfg['subsampling_factor']
    if z != 1:
        Z = np.diag((1/z, 1/z, 1))
        H1 = np.dot(Z, H1)
        H2 = np.dot(Z, H2)
        disp_m = np.floor(disp_m / z)
        disp_M = np.ceil(disp_M / z)
        hmargin = int(np.floor(hmargin / z))
        vmargin = int(np.floor(vmargin / z))

    # compute output images size
    roi = [[x, y], [x+w, y], [x+w, y+h], [x, y+h]]
    pts1 = common.points_apply_homography(H1, roi)
    x0, y0, w0, h0 = common.bounding_box2D(pts1)
    # check that the first homography maps the ROI in the positive quadrant
    np.testing.assert_allclose(np.round([x0, y0]), [hmargin, vmargin], atol=.01)

    # apply homographies and do the crops
    common.image_apply_homography(out1, im1, H1, w0 + 2*hmargin, h0 + 2*vmargin)
    common.image_apply_homography(out2, im2, H2, w0 + 2*hmargin, h0 + 2*vmargin)

    if cfg['disp_min'] is not None: disp_m = cfg['disp_min']
    if cfg['disp_max'] is not None: disp_M = cfg['disp_max']

    if block_matching.rectify_secondary_tile_only(cfg['matching_algorithm']):
        pts_in = [[0, 0], [disp_m, 0], [disp_M, 0]]
        pts_out = common.points_apply_homography(H1_inv,
                                                 pts_in)
        disp_m = pts_out[1,:] - pts_out[0,:]
        disp_M = pts_out[2,:] - pts_out[0,:]

    return H1, H2, disp_m, disp_M
